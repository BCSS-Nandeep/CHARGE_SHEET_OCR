"""Generate four synthetic charge-sheet PDFs with layout variations for testing.

These are NOT real police documents. They mimic structural variety only.
"""

from __future__ import annotations

from pathlib import Path

from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


def _page(c: canvas.Canvas, title: str) -> None:
    c.setFont("Helvetica-Bold", 14)
    c.drawString(50, 800, title)
    c.setFont("Helvetica", 10)


def make_digital_standard(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=A4)
    _page(c, "CHARGE SHEET / FINAL REPORT (u/s 173 CrPC)")
    y = 770
    lines = [
        "1. District                    : Sample District",
        "2. Police Station              : Central PS",
        "3. Year                        : 2024",
        "4. FIR No.                     : 123/2024",
        "5. Date of FIR                 : 12/03/2024",
        "6. Charge Sheet No.            : CS-45/2024",
        "7. Date of submission          : 20/06/2024",
        "8. Court                       : JMFC, Sample City",
        "9. Acts and Sections           : u/s 302, 34 IPC",
        "10. Final Report Type          : Charge Sheet",
        "11. Original / Supplementary   : Original",
        "12. Complainant                : Ramesh Kumar",
        "13. Informant                  : Ramesh Kumar",
        "14. Investigating Officer      : SI Anita Verma",
        "15. Rank of IO                 : Sub-Inspector",
        "",
        "Particulars of the Accused:",
        "1. Suresh Singh s/o Mohan Singh age 32 r/o Ward 4",
        "2. Vijay Patel s/o K. Patel age 28 r/o Market Road",
        "",
        "List of Witnesses:",
        "1. PW-1 Ramesh Kumar (complainant)",
        "2. PW-2 Dr. Mehta (medical officer)",
        "",
        "Property Seized: Knife, blood-stained cloth",
        "",
        "Brief Facts:",
        "On 12/03/2024 the complainant reported an assault. Investigation was taken up.",
        "",
        "Course of Investigation:",
        "Statements recorded, scene inspected, exhibits seized and sent to FSL.",
        "",
        "Conclusion:",
        "Sufficient evidence found to prosecute the accused under the cited sections.",
    ]
    for line in lines:
        c.drawString(50, y, line[:100])
        y -= 16
        if y < 60:
            c.showPage()
            y = 800
            c.setFont("Helvetica", 10)
    c.showPage()
    c.setFont("Helvetica", 10)
    c.drawString(50, 800, "--- PAGE 2 CONTINUATION ---")
    c.drawString(50, 780, "FSL Report: Pending / Sample Lab Report No. FSL-99")
    c.drawString(50, 760, "Jurisdiction: Sample Circle")
    c.save()


def make_alternate_labels(path: Path) -> None:
    """Different numbering and label wording."""
    c = canvas.Canvas(str(path), pagesize=A4)
    _page(c, "POLICE FINAL REPORT / CHALLAN")
    y = 770
    lines = [
        "Distt.                 Sample East",
        "P.S.                   Riverside Thana",
        "Crime No.              CR-778/2023",
        "F.I.R. Number          778/2023",
        "Year of FIR            2023",
        "Date of registration   05-11-2023",
        "Challan No.            CH-12/2024",
        "Forwarded on           01.02.2024",
        "Name of Court          Sessions Court, East",
        "Offence                Sections 420, 406 IPC & 66 IT Act",
        "Whether original or supplementary: Supplementary",
        "Name of complainant    Sunita Devi",
        "Name of IO             Inspector K. Rao",
        "",
        "List of Accused Persons",
        "1. Amit Joshi so R. Joshi aged 40 r/o Block C",
        "",
        "Particulars of Witnesses",
        "1. Sunita Devi",
        "2. Bank Manager, Sample Bank",
        "",
        "Muddemal / Articles seized: Laptop, fake documents",
        "",
        "Gist of the case: Cheating complaint regarding online fraud.",
        "Result of investigation: Charge sheet filed against accused.",
    ]
    for line in lines:
        c.drawString(50, y, line[:105])
        y -= 18
    c.save()


def make_multipage_sparse(path: Path) -> None:
    c = canvas.Canvas(str(path), pagesize=A4)
    _page(c, "FORM OF CHARGE-SHEET")
    c.drawString(50, 770, "District: North Zone")
    c.drawString(50, 750, "Police Station: Hill View")
    c.drawString(50, 730, "FIR No: 55/2022")
    c.drawString(50, 710, "Charge-sheet No: 9/2023")
    c.showPage()
    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, 800, "Accused")
    c.setFont("Helvetica", 10)
    c.drawString(50, 780, "1. Karan Mehta")
    c.drawString(50, 760, "2. Neha Shah")
    c.drawString(50, 740, "3. Unknown person (absconding)")
    c.showPage()
    c.setFont("Helvetica-Bold", 12)
    c.drawString(50, 800, "Witnesses")
    c.setFont("Helvetica", 10)
    c.drawString(50, 780, "1. Local shopkeeper")
    c.drawString(50, 760, "Acts and Sections: u/s 379 IPC")
    c.drawString(50, 740, "Brief Facts: Theft of two-wheeler reported and investigated.")
    c.drawString(50, 720, "Court: JMFC North")
    c.save()


def make_image_like_scanned(path: Path) -> None:
    """Create a PDF whose pages are images (forces OCR path / low native text)."""
    from reportlab.lib.utils import ImageReader
    from PIL import Image, ImageDraw, ImageFont

    c = canvas.Canvas(str(path), pagesize=A4)
    # Build an image with text drawn on it
    img = Image.new("RGB", (1240, 1754), color=(245, 245, 240))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 28)
        font_sm = ImageFont.truetype("arial.ttf", 22)
    except Exception:
        font = ImageFont.load_default()
        font_sm = font
    lines = [
        "CHARGE SHEET",
        "District: Coastal District",
        "Police Station: Harbour PS",
        "FIR No.: 901/2021",
        "Year: 2021",
        "Charge Sheet No.: CS-3/2022",
        "Date of FIR: 18/08/2021",
        "Court: Special Court",
        "u/s 307 IPC",
        "Complainant: Arjun Nair",
        "Investigating Officer: DySP L. Thomas",
        "Particulars of the Accused:",
        "1. Manoj Pillai s/o Pillai age 35",
        "List of Witnesses:",
        "1. Arjun Nair",
        "2. Constable Ravi",
        "Brief Facts: Assault case investigated and charge sheeted.",
        "Conclusion: Accused sent for trial.",
    ]
    y = 60
    for i, line in enumerate(lines):
        draw.text((60, y), line, fill=(20, 20, 20), font=font if i == 0 else font_sm)
        y += 40
    tmp = path.with_suffix(".png")
    img.save(tmp)
    c.drawImage(ImageReader(str(tmp)), 0, 0, width=A4[0], height=A4[1])
    c.showPage()
    c.save()
    tmp.unlink(missing_ok=True)


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    make_digital_standard(OUT / "sample_01_digital_standard.pdf")
    make_alternate_labels(OUT / "sample_02_alternate_labels.pdf")
    make_multipage_sparse(OUT / "sample_03_multipage.pdf")
    make_image_like_scanned(OUT / "sample_04_scanned_image.pdf")
    print(f"Wrote sample PDFs to {OUT}")


if __name__ == "__main__":
    main()
