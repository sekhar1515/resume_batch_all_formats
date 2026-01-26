# Resume Batch Tailoring (Common Formats + PDF output)

## What you get
- One tailored resume **per JD** (Markdown)
- Optional PDF export per tailored resume

## Supported base resume formats
- PDF (text-based)
- DOCX
- TXT / MD
- HTML
- PNG/JPG (OCR)

## Supported JD inputs
- URL (career pages / job boards)
- Local files: TXT/MD/DOCX/PDF/HTML
- PNG/JPG (OCR)

## Install
### Core
```bash
pip install -r requirements.txt
```

### Optional OCR (for scanned PDFs/images)
```bash
pip install -r requirements-ocr.txt
```

**OCR system tools needed**
- Tesseract OCR
- Poppler (for PDF->image conversion)

## Set API key
Copy `.env.example` to `.env` and set:
```
OPENAI_API_KEY="YOUR_KEY_HERE"
```

## Add multiple JDs
Edit `batch.csv`:
```csv
id,role,url,jd_file
1,QA_Automation_Engineer,https://boards.greenhouse.io/company/jobs/123456,
2,SDE2,,jds/jd_google_sde2.pdf
3,Backend_Engineer,https://jobs.lever.co/company/postingid,
```

## Run (outputs MD + PDF)
```bash
python batch_tailor.py --base my_resume.pdf --batch batch.csv --out ./output --model gpt-5.2 --temperature 0.2 --fetch_mode auto --emit_pdf
```

If blocked/JS-heavy URLs:
```bash
python batch_tailor.py --base SekharReddy.pdf --batch batch.csv --out ./output --model gpt-5.2 --temperature 0.2 --fetch_mode playwright --emit_pdf
```

If scanned/image resume or scanned PDFs:
```bash
python batch_tailor.py --base my_scanned_resume.pdf --batch batch.csv --out ./output --model gpt-5.2 --temperature 0.2 --emit_pdf --ocr
```

## Outputs
- `output/<id_role>__tailored.md`
- `output/<id_role>__tailored.pdf` (if `--emit_pdf`)
- `output/results.csv`
- `output/<id_role>__EXTRACTED_JD.txt` (saved when JD extraction is too short)



// Since the code is using openapi creds for billing 
// Running it locally using OLLAMA 
## run this command in bash
 ollama pull llama3.1:8b

## command to run the code 
 python3 batch_tailor.py --base sekharResume.tex --batch batch.csv --out ./output --model llama3.1:8b --fetch_mode playwright