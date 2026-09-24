"""
certificate.py — Kursni tugatgan o'quvchiga PDF sertifikat yaratadi.
"""

from pathlib import Path
from datetime import date
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.units import mm
from reportlab.lib.colors import HexColor, white, black
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

CERT_DIR = Path(__file__).resolve().parent / "certificates"
CERT_DIR.mkdir(exist_ok=True)

SUBJECT_LABELS = {
    "en": "English Language",
    "ru": "Russian Language",
}
LEVEL_LABELS = {
    "beginner": "Beginner (A1–A2)",
    "intermediate": "Intermediate (B1)",
    "advanced": "Advanced (B2+)",
}


def generate_certificate(
    full_name: str,
    subject: str,
    level: str,
    user_id: int,
) -> Path:
    """Chiroyli landscape A4 PDF sertifikat yaratadi va path qaytaradi."""
    safe_name = "".join(c if c.isalnum() or c in " _-" else "_" for c in full_name)[:40]
    filename = f"cert_{user_id}_{subject}_{level}_{date.today().isoformat()}.pdf"
    path = CERT_DIR / filename

    width, height = landscape(A4)
    c = canvas.Canvas(str(path), pagesize=landscape(A4))

    # Background
    c.setFillColor(HexColor("#0f172a"))
    c.rect(0, 0, width, height, fill=1, stroke=0)

    # Gold border
    gold = HexColor("#d4a017")
    c.setStrokeColor(gold)
    c.setLineWidth(3)
    margin = 15 * mm
    c.rect(margin, margin, width - 2 * margin, height - 2 * margin, fill=0, stroke=1)
    c.setLineWidth(1)
    c.rect(margin + 5 * mm, margin + 5 * mm, width - 2 * margin - 10 * mm, height - 2 * margin - 10 * mm, fill=0, stroke=1)

    # Title
    c.setFillColor(gold)
    c.setFont("Helvetica-Bold", 28)
    c.drawCentredString(width / 2, height - 45 * mm, "CERTIFICATE OF COMPLETION")

    c.setFillColor(HexColor("#94a3b8"))
    c.setFont("Helvetica", 12)
    c.drawCentredString(width / 2, height - 55 * mm, "Repetitorlik Telegram Bot — Language Course")

    # Body
    c.setFillColor(white)
    c.setFont("Helvetica", 14)
    c.drawCentredString(width / 2, height - 75 * mm, "This is to certify that")

    c.setFillColor(gold)
    c.setFont("Helvetica-Bold", 24)
    c.drawCentredString(width / 2, height - 90 * mm, full_name or "Student")

    c.setFillColor(white)
    c.setFont("Helvetica", 14)
    c.drawCentredString(width / 2, height - 105 * mm, "has successfully completed the")

    c.setFillColor(HexColor("#38bdf8"))
    c.setFont("Helvetica-Bold", 18)
    subj = SUBJECT_LABELS.get(subject, subject)
    lvl = LEVEL_LABELS.get(level, level)
    c.drawCentredString(width / 2, height - 118 * mm, f"{subj} — {lvl}")

    c.setFillColor(HexColor("#94a3b8"))
    c.setFont("Helvetica", 11)
    c.drawCentredString(
        width / 2,
        height - 135 * mm,
        f"Issued on {date.today().strftime('%d %B %Y')}",
    )

    # Footer line
    c.setStrokeColor(gold)
    c.setLineWidth(0.5)
    c.line(width / 2 - 60 * mm, 35 * mm, width / 2 + 60 * mm, 35 * mm)
    c.setFillColor(HexColor("#64748b"))
    c.setFont("Helvetica", 9)
    c.drawCentredString(width / 2, 28 * mm, "Repetitor Bot  •  Keep learning every day")

    c.save()
    return path
