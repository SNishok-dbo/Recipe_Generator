"""PDF export utility for generated recipes.

Converts Markdown recipe text into a clean, downloadable PDF using fpdf2.
"""

import re


def _clean(text: str) -> str:
    """Strip Markdown decoration and replace non-Latin-1 characters."""
    # Common replacements first
    replacements = {
        "\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"',
        "\u2013": "-", "\u2014": "--", "\u00a3": "GBP",
        "\u2705": "[OK]", "\u26a0": "[!]", "\u2192": "->",
        "\u2022": "*", "\u00b0": "deg",
    }
    out: list[str] = []
    for ch in text:
        ch = replacements.get(ch, ch)
        try:
            ch.encode("latin-1")
            out.append(ch)
        except UnicodeEncodeError:
            # Replace any remaining non-latin-1 (emoji etc.) with nothing
            out.append("")
    cleaned = "".join(out)
    cleaned = re.sub(r"[*_~`]+", "", cleaned)
    return cleaned.strip()


def recipe_to_pdf(recipe_text: str, recipe_name: str) -> bytes:
    """Convert Markdown *recipe_text* to PDF bytes. Never raises — falls back to a text-only PDF."""
    try:
        from fpdf import FPDF
    except ImportError:
        return recipe_text.encode("utf-8")

    try:
        class _RecipePDF(FPDF):
            def __init__(self, name: str):
                super().__init__()
                self._recipe_name = name

            def header(self):
                self.set_font("Helvetica", "B", 10)
                self.set_text_color(120, 120, 120)
                self.cell(0, 8, "Budget Recipe Chatbot", new_x="LMARGIN", new_y="NEXT", align="R")
                self.set_text_color(0, 0, 0)

            def footer(self):
                self.set_y(-12)
                self.set_font("Helvetica", "I", 8)
                self.set_text_color(150, 150, 150)
                self.cell(0, 6, f"Page {self.page_no()} | {_clean(self._recipe_name)}", align="C")

        pdf = _RecipePDF(recipe_name)
        pdf.set_auto_page_break(auto=True, margin=18)
        pdf.add_page()
        pdf.set_left_margin(20)
        pdf.set_right_margin(20)

        W = pdf.w - pdf.l_margin - pdf.r_margin

        # Title
        pdf.set_font("Helvetica", "B", 18)
        pdf.set_text_color(40, 100, 40)
        pdf.multi_cell(W, 12, _clean(recipe_name), align="C")
        pdf.set_text_color(0, 0, 0)
        pdf.ln(3)
        pdf.set_draw_color(40, 100, 40)
        pdf.set_line_width(0.8)
        pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
        pdf.ln(5)

        for raw_line in recipe_text.split("\n"):
            line = raw_line.rstrip()
            pdf.set_x(pdf.l_margin)

            if line.startswith("## "):
                pdf.set_font("Helvetica", "B", 14)
                pdf.set_text_color(30, 80, 30)
                pdf.ln(3)
                pdf.multi_cell(W, 9, _clean(line[3:]))
                pdf.set_text_color(0, 0, 0)
            elif line.startswith("### "):
                pdf.set_font("Helvetica", "B", 11)
                pdf.set_text_color(60, 60, 60)
                pdf.ln(2)
                pdf.multi_cell(W, 7, _clean(line[4:]))
                pdf.set_text_color(0, 0, 0)
            elif line.startswith("#"):
                pdf.set_font("Helvetica", "B", 12)
                pdf.multi_cell(W, 8, _clean(line.lstrip("#").strip()))
            elif "|" in line and "---" not in line and line.strip().startswith("|"):
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                clean_cells = [_clean(c) for c in cells if c.strip()]
                if clean_cells:
                    pdf.set_font("Helvetica", size=9)
                    pdf.multi_cell(W, 5, "  |  ".join(clean_cells))
            elif line.startswith(("- ", "* ")):
                pdf.set_font("Helvetica", size=10)
                pdf.multi_cell(W, 6, "  -  " + _clean(line[2:]))
            elif re.match(r"^\d+\. ", line):
                pdf.set_font("Helvetica", size=10)
                pdf.multi_cell(W, 6, "  " + _clean(line))
            elif line.strip() == "---":
                pdf.ln(2)
                pdf.set_draw_color(180, 180, 180)
                pdf.set_line_width(0.3)
                pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
                pdf.ln(4)
            elif line.strip():
                pdf.set_font("Helvetica", size=10)
                pdf.multi_cell(W, 6, _clean(line))
            else:
                pdf.ln(2)

        return bytes(pdf.output())

    except Exception:
        # Last-resort: wrap plain text in a minimal PDF
        try:
            pdf2 = FPDF()
            pdf2.add_page()
            pdf2.set_font("Helvetica", size=10)
            for raw_line in recipe_text.split("\n"):
                pdf2.multi_cell(0, 6, _clean(raw_line) or " ")
            return bytes(pdf2.output())
        except Exception:
            return recipe_text.encode("utf-8")
