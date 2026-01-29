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
import time

from pathlib import Path
from string import Template

import requests
import trafilatura
from bs4 import BeautifulSoup

from string import Template

PROMPT_TEMPLATE = Template(r"""
You are an ATS resume editor AND a strict LaTeX editor.

CRITICAL OUTPUT RULE (ZERO TOLERANCE)

Output MUST be LaTeX ONLY.

Output MUST be the FULL updated BASE CONTENT block (not a diff).

Output MUST start with an existing LaTeX command from BASE CONTENT.

Do NOT output prose, explanations, markers, or markdown.

PRIMARY OBJECTIVE
Match the JD AND force a single-page resume by reducing vertical space through bullet clubbing and aggressive de-duplication, without changing any locked structure.

HARD LOCKS (MUST NOT CHANGE)

Do NOT change any \resumeSubheading{...}{...}{...}{...} lines (byte-for-byte).

Do NOT remove or reorder roles/sections.

Do NOT change links (\href), contact info, headings, or LaTeX macros/environments.

Do NOT move bullets between roles.

ONLY ALLOWED EDITS
A) Bullet DESCRIPTION text only:

\resumeItem{TITLE}{DESCRIPTION} -> edit ONLY DESCRIPTION

\resumeSubItem{TITLE}{DESCRIPTION} -> edit ONLY DESCRIPTION

\item <text> -> edit ONLY the text portion

B) Skills section text ONLY (within the existing Skills block/lines).

STRICT SINGLE-PAGE ENFORCEMENT (NON-NEGOTIABLE)

Do NOT increase the total number of bullets anywhere.

Reduce bullets per role to a MAX of 3 (unless the role already has fewer).

If a role has >3 bullets, CLUB/MERGE them by combining content into 2–3 dense bullets using semicolons/commas.

Each remaining bullet must be 1 line if possible and at most 2 lines (aim <= 160 characters when feasible).

Remove repeated phrasing, generic verbs, and duplicate tech mentions across bullets in the same role.

Compress Skills to 2–4 compact grouped lines (comma-separated), prioritizing JD keywords first.

If still long, further compress by merging similar bullets (within the same role only) and shortening phrasing.

JD ALIGNMENT RULES

Extract the JD’s top responsibilities, tools, and ATS keywords.

Rewrite EVERY bullet to reflect JD language, ownership, and outcomes while staying faithful to the base meaning.

Add ATS synonyms/expansions naturally (API development/integration, distributed systems, CI/CD pipelines, containerization/orchestration, automated testing, observability, security, performance, scalability).

METRICS & REALISM

Preserve existing numbers exactly.

You MAY add new numbers ONLY when the original bullet has none, and keep them plausible and minimal (e.g., 10–30% improvements, hours saved/week) without contradicting the base claim.

Do not add more than ONE new metric per bullet.

SKILLS OPTIMIZATION

Add missing JD skills into Skills.

Do NOT remove any existing skills.

Reorder/group for ATS relevance + space efficiency (JD-first, then supporting tools).

QUALITY GATES BEFORE OUTPUT

All \resumeSubheading lines unchanged.

Sections/roles unchanged and in same order.

Bullet count not increased; bullets per role max 3 (unless already fewer).

All bullets rewritten and aligned to JD.

LaTeX compiles (escape %, &, _, #, $, {, }, ^, ~, ).

LOCKED SUBHEADINGS (COPY EXACTLY; DO NOT MODIFY)
$locked_subheadings

TARGET ROLE:
$role

JOB DESCRIPTION:
$jd_text

BASE LATEX CONTENT (return FULL updated content; edit only allowed parts):
$base_content

OUTPUT
Return ONLY the FULL updated LaTeX for BASE CONTENT.
""")
def split_latex_flexible(tex: str):
    """
    Works for BOTH:
    - full latex doc: has \begin{document} ... \end{document}
    - fragment: no \begin{document}

    Returns:
      (preamble_and_begin_doc, protected_header_block, editable_body, end_doc_or_empty)

    If fragment: preamble="", end="" and editable_body=whole file minus protected header (if detected).
    """
    tex = (tex or "").replace("\r\n", "\n").replace("\r", "\n").strip()

    # Try full doc first
    m_begin = re.search(r"\\begin\{document\}", tex)
    m_end = re.search(r"\\end\{document\}", tex)

    if m_begin and m_end and m_end.start() > m_begin.end():
        pre = tex[:m_begin.end()].strip()
        body = tex[m_begin.end():m_end.start()].strip()
        post = tex[m_end.start():].strip()
    else:
        # Fragment mode
        pre = ""
        body = tex
        post = ""

    # Protect contact header if your template has a top tabular*
    # (This is OPTIONAL protection; if not found, nothing breaks)
    header_match = re.search(
        r"(\\begin\{tabular\*\}.*?\\end\{tabular\*\})",
        body,
        flags=re.DOTALL
    )
    if header_match:
        header_block = header_match.group(1).strip()
        editable_body = (body[:header_match.start()] + "\n\n" + body[header_match.end():]).strip()
        # ^ keep content before/after header editable EXCEPT the header itself
    else:
        header_block = ""
        editable_body = body.strip()

    return pre, header_block, editable_body, post


def stitch_latex_flexible(pre: str, header_block: str, editable_body: str, post: str) -> str:
    """
    Rebuilds:
    - Full doc if pre/post exist
    - Fragment if they are empty
    """
    parts = []
    if pre:
        parts.append(pre)
    if header_block:
        parts.append(header_block)
    if editable_body:
        parts.append(editable_body)
    if post:
        parts.append(post)
    return "\n\n".join(parts).strip() + "\n"



# ----------------- helpers -----------------
def enforce_metric_placeholders_in_bullets(tex_block: str) -> str:
    # For common item patterns: \item ... or \resumeItem{...}{...}
    lines = tex_block.splitlines()
    out = []
    for line in lines:
        l = line

        # If it's a bullet and has no number/percent and no placeholder -> append placeholder
        is_bullet = ("\\item" in l) or ("\\resumeItem" in l)
        has_metric = bool(re.search(r"\d|%|\\textbf\{\[ADD (METRIC|SCALE|IMPACT)\]\}", l))
        if is_bullet and (not has_metric):
            # append safely at end
            if l.rstrip().endswith("}"):
                l = l.rstrip()[:-1]   # keep braces balanced for common patterns
            else:
                l = l.rstrip() 
        out.append(l)
    return "\n".join(out)

def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)

def slugify(s: str) -> str:
    s = (s or "").strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s[:80] if s else "resume"

def call_llm_ollama(prompt: str, model: str, timeout_sec: int = 240) -> str:
    """
    Robust local call:
    - Timeout so it doesn't hang on a JD
    - Clear error message on failure
    """
    try:
        p = subprocess.run(
            ["ollama", "run", model],
            input=prompt.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_sec
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"Ollama timed out after {timeout_sec}s. Try a smaller JD (reduce_jd) or a smaller model.")

    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", errors="ignore") or "Ollama returned non-zero exit code.")
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


STOPWORDS = set("""
a an the and or to of in for with on at by from as is are be been being this that these those
""".split())

def reduce_jd(jd: str, max_chars: int = 12000) -> str:
    jd = (jd or "").strip()
    if len(jd) <= max_chars:
        return jd
    # keep top + bottom (often contains responsibilities + qualifications)
    return jd[:8000] + "\n...\n" + jd[-4000:]

def looks_like_bad_jd(text: str) -> bool:
    t = (text or "").lower()
    bad_signals = [
        "enable javascript", "cookies", "sign in", "access denied",
        "robot check", "captcha", "forbidden", "not authorized"
    ]
    return any(x in t for x in bad_signals)

def extract_keywords(jd: str, top_n: int = 40) -> list[str]:
    # lightweight keyword extractor (no external libs)
    words = re.findall(r"[A-Za-z][A-Za-z0-9\+\#\.\-]{1,}", jd or "")
    freq = {}
    for w in words:
        lw = w.lower()
        if lw in STOPWORDS or len(lw) < 3:
            continue
        freq[lw] = freq.get(lw, 0) + 1
    ranked = sorted(freq.items(), key=lambda x: (-x[1], x[0]))
    return [w for w, _ in ranked[:top_n]]

def clean_llm_return(text: str) -> str:
    if not text:
        return ""
    t = text.strip()
    t = re.sub(r"^```(?:latex|tex)?\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


def clean_llm_latex_output(tex: str, fallback_base: str) -> str:
    """
    Ensures we always save compilable LaTeX:
    - strips ``` wrappers
    - if model returns only body: stitches into base template
    - if model returns broken doc: fall back to base
    """
    if not tex:
        return fallback_base

    t = tex.strip()

    # Remove common wrappers
    t = re.sub(r"^```(?:latex|tex)?\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"\s*```$", "", t)

    # normalize line endings
    t = t.replace("\r\n", "\n").replace("\r", "\n").strip()

    # If returned only body, stitch into base template
    if "\\begin{document}" not in t and "\\end{document}" not in t:
        pre, body, post = split_latex_document(fallback_base)
        if pre and post:
            # Replace only the marked region (most robust)
            if "% === RESUME_CONTENT_START ===" in fallback_base and "% === RESUME_CONTENT_END ===" in fallback_base:
                body2 = re.sub(
                    r"% === RESUME_CONTENT_START ===.*?% === RESUME_CONTENT_END ===",
                    "% === RESUME_CONTENT_START ===\n" + t + "\n% === RESUME_CONTENT_END ===",
                    body,
                    flags=re.DOTALL
                )
                return pre + body2 + post
            # Otherwise, insert body as-is
            return pre + "\n" + t + "\n" + post
        return fallback_base

    # If has begin but missing end (broken), fall back
    if "\\begin{document}" in t and "\\end{document}" not in t:
        return fallback_base

    return t

def fetch_and_extract_jd_from_url_with_retry(url: str, fetch_mode: str, retries: int = 2) -> str:
    last_err = None
    for i in range(retries + 1):
        try:
            return fetch_and_extract_jd_from_url(url, fetch_mode)  # <-- FIX
        except Exception as e:
            last_err = e
            time.sleep(1.5 * (i + 1))
    raise last_err




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

def fetch_with_playwright(url: str, timeout_ms: int = 25000) -> str:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        page.wait_for_timeout(400)
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

def add_metric_placeholders_safely(tex_block: str) -> str:
    """
    Only touches obvious bullets:
    - \item ...
    - \resumeItem{X}{Y}
    Adds \textbf{[ADD METRIC]} only if no digit/% and no placeholder exists.
    """
    out = []
    for line in tex_block.splitlines():
        l = line

        # Handle \resumeItem{...}{...}
        m = re.search(r"(\\resumeItem\{[^{}]*\}\{)(.*)(\}\s*)$", l)
        if m:
            body = m.group(2)
            has_metric = bool(re.search(r"\d|%|\\textbf\{\[ADD (METRIC|SCALE|IMPACT)\]\}", body))
            if not has_metric:
                body = body.rstrip()
            l = m.group(1) + body + m.group(3)
            out.append(l)
            continue

        # Handle \item ...
        if re.search(r"^\s*\\item\b", l):
            has_metric = bool(re.search(r"\d|%|\\textbf\{\[ADD (METRIC|SCALE|IMPACT)\]\}", l))
            if not has_metric:
                l = l.rstrip() 
            out.append(l)
            continue

        out.append(l)

    return "\n".join(out)


def generate_tailored_body(model: str, role: str, source: str, jd_text: str, base_content: str) -> str:
    jd_text = reduce_jd(jd_text, max_chars=8000)
    keywords = extract_keywords(jd_text, top_n=40)

    prompt = PROMPT_TEMPLATE.safe_substitute(
    role=role or "",
    source=source or "",
    keywords=", ".join(keywords),
    jd_text=jd_text,
    base_content=base_content
    )


    new_body = call_llm_ollama(prompt, model=model).strip()
    new_body = clean_llm_return(new_body)

    # Optional safety: add placeholders without breaking braces
    new_body = add_metric_placeholders_safely(new_body)

    # If model returned junk/empty, fall back
    if len(new_body) < 200:
        return base_content

    return new_body





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
    pre, header_block, editable_body, post = split_latex_flexible(base_tex)

    if len(base_tex) < 400:
        raise SystemExit("ERROR: Base LaTeX is too short or unreadable.")

    results = []
    with batch_path.open("r", encoding="utf-8", errors="ignore", newline="") as f:
        reader = csv.DictReader(f)
        for idx, row in enumerate(reader, start=1):
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
                    jd_text = fetch_and_extract_jd_from_url_with_retry(url, args.fetch_mode, retries=2)
                else:
                    raise ValueError("No url or jd_file provided")
            except Exception as e:
                results.append({"id": job_id, "role": role, "source": source, "status": "FAIL", "reason": str(e), "output_tex": ""})
                print(f"FAIL: {e}")
                continue

            jd_text = (jd_text or "").strip()
            if looks_like_bad_jd(jd_text):
                debug = f"{slugify(f'{job_id}_{role}') or 'job'}__BAD_JD.txt"
                (out_dir / debug).write_text(jd_text + "\n", encoding="utf-8")
                results.append({"id": job_id, "role": role, "source": source, "status": "SKIP",
                                "reason": f"JD looks blocked/cookiewall. Saved {debug}", "output_tex": ""})
                print("SKIP: JD looks blocked/cookiewall.")
                continue

            if len(jd_text) < 700:
                debug = f"{slugify(f'{job_id}_{role}') or 'job'}__EXTRACTED_JD.txt"
                (out_dir / debug).write_text(jd_text + "\n", encoding="utf-8")
                results.append({"id": job_id, "role": role, "source": source, "status": "SKIP", "reason": f"JD too short ({len(jd_text)} chars). Saved {debug}", "output_tex": ""})
                print(f"SKIP: JD too short ({len(jd_text)} chars). Saved extracted text.")
                continue

            # 2) Generate tailored TeX
            base_name = slugify(f"{job_id}_{role}") if (job_id or role) else "job"
            out_tex = f"{base_name}_{idx:03d}__tailored.tex"


            try:
                # Base is split once
                base_tex = read_any_text(base_path)
                pre, header_block, editable_body, post = split_latex_flexible(base_tex)

                ...

                # Per JD
                tailored_body = generate_tailored_body(args.model, role, source, jd_text, editable_body)
                final_tex = stitch_latex_flexible(pre, header_block, tailored_body, post)

                (out_dir / out_tex).write_text(final_tex, encoding="utf-8")


                print(f"Saved TEX: {out_dir / out_tex}")

                # print(f"Saved TEX: {out_dir / out_tex}")
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
