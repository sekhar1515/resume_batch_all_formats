#!/usr/bin/env python3
"""
Batch resume tailoring: LaTeX in -> LaTeX out (Overleaf-ready)

- Base resume: .tex (Overleaf template + content)
- JD input: URL (career pages) or local files (.txt/.md/.pdf/.docx/.html)
- Output: one tailored .tex per JD in /output

Run:
  python3 batch_tailor.py --base resume.tex --batch batch.csv --out ./output --model llama3.1:8b --fetch_mode auto
"""

import argparse
import csv
import json
import re
import subprocess
from pathlib import Path

import requests
import trafilatura
from bs4 import BeautifulSoup


PROMPT_TEMPLATE = r"""
You are an expert ATS Resume Writer and LaTeX editor.

TASK
Tailor the resume to the Job Description (JD) by editing the provided LaTeX resume source.

HARD RULES (Template safety + no hallucination)
1) DO NOT invent or add anything not explicitly present in the base LaTeX resume content.
   - No new companies, job titles, projects, certifications, degrees, dates, tools, or achievements.
   - Do not change employment dates.
2) Preserve ALL links/contact details exactly as in the base LaTeX (email/phone/LinkedIn/GitHub/portfolio/URLs).
   - Do not remove or alter URLs.
3) Preserve the LaTeX template and macros:
   - Do not change the preamble (documentclass/packages/custom commands).
   - Do not rename or remove macros.
   - Keep the same structure and formatting style.
4) If the JD requests something not present, do NOT fabricate it.
   - Either omit it or add: \textbf{[ADD RELEVANT EXPERIENCE HERE]}.
5) Metrics:
   - Use only metrics already present.
   - If missing, use \textbf{[ADD METRIC]} (do NOT guess numbers).

ATS ALIGNMENT RULES
- Rewrite bullets to mirror JD keywords ONLY when supported by existing resume content.
- Prioritize the most relevant bullets per role (reorder within a role is allowed).
- Do not delete entire roles.

SPECIAL EDITING RULE
Only modify text between:
% === RESUME_CONTENT_START ===
and
% === RESUME_CONTENT_END ===

OUTPUT
- Return ONLY the full updated LaTeX source code.
- Must compile in Overleaf.
- Do NOT use Markdown. Do NOT wrap in triple backticks.
"""


# ----------------- helpers -----------------

def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def slugify(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:80] if s else "resume"

def call_llm_ollama(prompt: str, model: str) -> str:
    p = subprocess.run(
        ["ollama", "run", model],
        input=prompt.encode("utf-8"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", errors="ignore"))
    return p.stdout.decode("utf-8", errors="ignore").strip()


# ----------------- latex markers -----------------

def split_latex_document(tex: str):
    m1 = re.search(r"\\begin\\{document\\}", tex)
    m2 = re.search(r"\\end\\{document\\}", tex)
    if not (m1 and m2):
        return "", tex, ""
    pre = tex[:m1.end()]
    body = tex[m1.end():m2.start()]
    post = tex[m2.start():]
    return pre, body, post

def wrap_body_with_markers(tex: str) -> str:
    pre, body, post = split_latex_document(tex)
    if not pre:
        return tex
    if "RESUME_CONTENT_START" in tex and "RESUME_CONTENT_END" in tex:
        return tex
    return pre + "\n% === RESUME_CONTENT_START ===\n" + body + "\n% === RESUME_CONTENT_END ===\n" + post


# ----------------- read JD/resume -----------------

def read_text_file(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore").strip()

def extract_text_from_docx(path: Path) -> str:
    from docx import Document
    doc = Document(str(path))
    parts = []
    for p in doc.paragraphs:
        t = (p.text or "").strip()
        if t:
            parts.append(t)
    return "\n".join(parts).strip()

def extract_text_from_pdf(path: Path) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    parts = []
    for page in reader.pages:
        parts.append(page.extract_text() or "")
    return "\n".join(parts).strip()

def extract_text_from_html_file(path: Path) -> str:
    html = path.read_text(encoding="utf-8", errors="ignore")
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()

def read_any_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in (".tex", ".md", ".txt"):
        return read_text_file(path)
    if ext == ".docx":
        return extract_text_from_docx(path)
    if ext == ".pdf":
        return extract_text_from_pdf(path)
    if ext in (".html", ".htm"):
        return extract_text_from_html_file(path)
    raise ValueError(f"Unsupported file format: {ext}")


# ----------------- JD from URL -----------------

def fetch_html_requests(url: str, timeout: int = 25) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9",
    }
    r = requests.get(url, headers=headers, timeout=timeout)
    r.raise_for_status()
    return r.text

def fetch_with_playwright(url: str, timeout_ms: int = 45000) -> str:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        page.wait_for_timeout(1800)
        html = page.content()
        browser.close()
        return html

def extract_jsonld_jobposting(html: str):
    soup = BeautifulSoup(html, "html.parser")
    scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
    for s in scripts:
        raw = (s.string or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        items = data if isinstance(data, list) else [data]
        for obj in items:
            if isinstance(obj, dict):
                t = obj.get("@type")
                if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                    desc = obj.get("description") or ""
                    return BeautifulSoup(desc, "html.parser").get_text("\n").strip()
    return None

def extract_text_trafilatura(html: str, url: str = "") -> str:
    return (trafilatura.extract(html, url=url, include_tables=True, favor_recall=True) or "").strip()

def extract_text_bs4(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "footer", "header", "nav", "form"]):
        tag.decompose()
    text = soup.get_text("\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()

def fetch_and_extract_jd_from_url(url: str, fetch_mode: str) -> str:
    def extract_from_html(html: str) -> str:
        jl = extract_jsonld_jobposting(html)
        if jl and len(jl) > 600:
            return jl
        t = extract_text_trafilatura(html, url=url)
        if t and len(t) > 800:
            return t
        return extract_text_bs4(html)

    html = fetch_html_requests(url)
    text = extract_from_html(html)

    if fetch_mode in ("auto", "playwright") and len(text) < 800:
        try:
            html2 = fetch_with_playwright(url)
            text2 = extract_from_html(html2)
            if len(text2) > len(text):
                return text2
        except Exception:
            pass

    return text


# ----------------- LLM prompt assembly -----------------

def generate_tailored_tex(model: str, role: str, source: str, jd_text: str, base_tex: str) -> str:
    prompt = PROMPT_TEMPLATE + f"""

TARGET ROLE: {role or ""}
JOB SOURCE: {source or ""}

JOB DESCRIPTION:
<<<JD_START
{jd_text}
JD_END>>>

BASE LATEX RESUME (edit this; preserve template/macros):
<<<LATEX_START
{base_tex}
LATEX_END>>>
"""
    return call_llm_ollama(prompt, model=model).strip()


# ----------------- main -----------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="Base resume LaTeX file (.tex)")
    ap.add_argument("--batch", required=True, help="CSV with rows: id,role,url,jd_file")
    ap.add_argument("--out", default="./output", help="Output folder")
    ap.add_argument("--model", default="llama3.1:8b", help="Ollama model (e.g., llama3.1:8b)")
    ap.add_argument("--fetch_mode", default="auto", choices=["auto", "requests", "playwright"], help="Fetch mode for URLs")
    args = ap.parse_args()

    base_path = Path(args.base)
    batch_path = Path(args.batch)
    out_dir = Path(args.out)

    if not base_path.exists():
        raise SystemExit(f"ERROR: Base resume not found: {base_path}")
    if not batch_path.exists():
        raise SystemExit(f"ERROR: batch.csv not found: {batch_path}")

    ensure_dir(out_dir)

    base_tex = read_any_text(base_path)
    base_tex = wrap_body_with_markers(base_tex)

    if len(base_tex) < 400:
        raise SystemExit("ERROR: Base LaTeX is too short or unreadable.")

    results = []
    with batch_path.open("r", encoding="utf-8", errors="ignore", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            job_id = (row.get("id") or "").strip()
            role = (row.get("role") or "").strip()
            url = (row.get("url") or "").strip()
            jd_file = (row.get("jd_file") or "").strip()
            source = url if url else jd_file

            print(f"\nProcessing: id={job_id} role={role}")

            # 1) Get JD text
            try:
                if jd_file:
                    p = Path(jd_file)
                    if not p.exists():
                        raise FileNotFoundError(f"jd_file not found: {jd_file}")
                    jd_text = read_any_text(p)
                elif url:
                    jd_text = fetch_and_extract_jd_from_url(url, args.fetch_mode)
                else:
                    raise ValueError("No url or jd_file provided")
            except Exception as e:
                results.append({"id": job_id, "role": role, "source": source, "status": "FAIL", "reason": str(e), "output_tex": ""})
                print(f"FAIL: {e}")
                continue

            jd_text = (jd_text or "").strip()
            if len(jd_text) < 700:
                debug = f"{slugify(f'{job_id}_{role}') or 'job'}__EXTRACTED_JD.txt"
                (out_dir / debug).write_text(jd_text + "\n", encoding="utf-8")
                results.append({"id": job_id, "role": role, "source": source, "status": "SKIP", "reason": f"JD too short ({len(jd_text)} chars). Saved {debug}", "output_tex": ""})
                print(f"SKIP: JD too short ({len(jd_text)} chars). Saved extracted text.")
                continue

            # 2) Generate tailored TeX
            base_name = slugify(f"{job_id}_{role}") if (job_id or role) else "job"
            out_tex = f"{base_name}__tailored.tex"

            try:
                tailored_tex = generate_tailored_tex(args.model, role, source, jd_text, base_tex)
                (out_dir / out_tex).write_text(tailored_tex + "\n", encoding="utf-8")
                print(f"Saved TEX: {out_dir / out_tex}")
                results.append({"id": job_id, "role": role, "source": source, "status": "OK", "reason": "", "output_tex": out_tex})
            except Exception as e:
                results.append({"id": job_id, "role": role, "source": source, "status": "FAIL", "reason": f"LLM error: {e}", "output_tex": ""})
                print(f"FAIL: {e}")

    # results report
    report_path = out_dir / "results.csv"
    with report_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["id", "role", "source", "status", "reason", "output_tex"])
        writer.writeheader()
        writer.writerows(results)

    print(f"\nBatch complete. Report: {report_path}")


if __name__ == "__main__":
    main()
