"""OCR layout reconstruction from positioned boxes (no real document data)."""

from app.services.layout_service import Box, layout_from_boxes


def B(text, x0, y0, x1, y1, conf=0.99):
    return Box(text, x0, y0, x1, y1, conf)


def test_header_table_pairs_labels_with_values_below():
    boxes = [
        B("DISTRICT", 300, 100, 460, 130), B("YEAR", 540, 100, 630, 130),
        B("P.S.", 760, 100, 830, 130), B("FIR. No.", 1000, 100, 1120, 130), B("DATED", 1280, 100, 1400, 130),
        B("NORTHTOWN", 320, 140, 450, 170), B("2017", 545, 140, 630, 170), B("RIVERSIDE", 730, 140, 880, 170),
        B("4/2017", 1000, 140, 1130, 170), B("02.03.2017", 1260, 140, 1420, 170),
    ]
    lay = layout_from_boxes(boxes, page=1)
    pairs = {(p.label, p.value) for p in lay.header_pairs}
    assert pairs == {
        ("DISTRICT", "NORTHTOWN"), ("YEAR", "2017"), ("P.S.", "RIVERSIDE"),
        ("FIR. No.", "4/2017"), ("DATED", "02.03.2017"),
    }


def test_rows_split_label_value_and_role_columns():
    boxes = [
        B("12", 225, 1000, 268, 1033), B("Particulars of the accused", 290, 1000, 650, 1033),
        B("persons charge sheeted", 290, 1040, 630, 1073),
        B("A-1) John Doe S/o Richard, Age: 30 years,", 680, 1000, 1440, 1033),
        B("R/o Northtown.", 680, 1040, 900, 1073),
        B("A-2) Jane Roe W/o Mark, Age: 28", 680, 1120, 1440, 1153),  # second accused, same cell
        B("13", 225, 1200, 268, 1233), B("Particulars of the witnesses", 290, 1200, 800, 1233),
        B("1", 235, 1250, 258, 1280), B("Sri. A. Witness, PC-12 of Riverside PS", 290, 1250, 1050, 1280),
        B("Assisted to IO", 1160, 1250, 1350, 1280),
    ]
    lay = layout_from_boxes(boxes, page=1)
    rows = {r.cells[0]: r.cells[1:] for r in lay.rows}
    assert rows["12"][0] == "Particulars of the accused\npersons charge sheeted"
    assert "A-1) John Doe" in rows["12"][1] and "A-2) Jane Roe" in rows["12"][1]
    assert rows["1"] == ["Sri. A. Witness, PC-12 of Riverside PS", "Assisted to IO"]


def test_label_starting_above_its_serial_is_not_merged_into_previous_row():
    boxes = [
        B("04", 228, 443, 272, 477), B("Sections of Law & Act", 290, 445, 596, 475),
        B("379 IPC", 690, 445, 1011, 476),
        B("Charge Sheet /Final", 295, 488, 580, 519),  # starts 16px above its serial
        B("05", 227, 504, 273, 541), B("Charge Sheet", 689, 505, 880, 539), B("Report", 291, 526, 394, 565),
        B("06", 228, 568, 272, 602), B("If Final Report", 291, 568, 496, 603), B("Nil", 683, 565, 735, 603),
    ]
    rows = {r.cells[0]: r.cells[1:] for r in layout_from_boxes(boxes, page=1).rows}
    assert rows["04"] == ["Sections of Law & Act", "379 IPC"]
    assert rows["05"] == ["Charge Sheet /Final\nReport", "Charge Sheet"]
    assert rows["06"] == ["If Final Report", "Nil"]


def test_value_beside_tall_label_joins_its_row():
    boxes = [
        B("10", 228, 843, 270, 876), B("Date on which the", 290, 844, 548, 874),
        B("complainant was", 290, 887, 528, 917), B("02.03.2017", 683, 884, 846, 920),
        B("informed of the result.", 290, 925, 598, 958),
        B("11", 226, 968, 267, 1001), B("Details of properties", 289, 969, 573, 1002),
    ]
    rows = {r.cells[0]: r.cells[1:] for r in layout_from_boxes(boxes, page=1).rows}
    assert rows["10"][-1] == "02.03.2017"


def test_skewed_tall_boxes_keep_reading_order():
    # Curved page: boxes taller than the line pitch overlap their neighbours
    lines = [f"line {i} of the narrative paragraph text" for i in range(6)]
    boxes = [B(t, 300, 1000 + 45 * i, 1450, 1065 + 45 * i) for i, t in enumerate(lines)]
    lay = layout_from_boxes(boxes, page=2)
    assert lay.text.splitlines() == lines


def test_low_confidence_specks_dropped():
    boxes = [B("Real text line here", 100, 100, 600, 130), B("'0", 1500, 300, 1520, 330, conf=0.55)]
    assert "'0" not in layout_from_boxes(boxes, page=1).text
