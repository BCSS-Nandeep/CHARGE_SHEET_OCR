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


def test_page_wide_lines_under_last_row_label_stay_out_of_the_row():
    # Last form row's label, then a court heading and narrative that start inside
    # the label column but run across the page
    boxes = [
        B("17", 228, 200, 270, 232), B("Brief facts of the case", 290, 200, 600, 232),
        B("IN THE COURT OF THE MAGISTRATE AT NORTHTOWN", 348, 244, 1392, 290),
        B("Honored Sir,", 314, 305, 493, 341),
        B("This is a case of theft which occurred on 02.03.2017 at about", 300, 380, 1450, 420),
        B("1500 hrs near the market where the accused took the bicycle", 300, 430, 1450, 470),
    ]
    lay = layout_from_boxes(boxes, page=2)
    rows = {r.cells[0]: r.cells[1:] for r in lay.rows}
    assert rows["17"] == ["Brief facts of the case"]
    free = [t for _, t in lay.free_lines]
    assert free[0].startswith("IN THE COURT OF") and free[-1].startswith("1500 hrs")


def test_rotated_page_is_straightened_and_bleed_through_dropped():
    import math

    from app.services.layout_service import boxes_from_ocr
    from app.services.ocr_service import OcrLine

    angle = math.radians(3.0)  # page photographed 3° clockwise

    def rotated(text, x0, y0, x1, y1):
        cx, cy = 800, 1000
        pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        poly = [(cx + (x - cx) * math.cos(angle) - (y - cy) * math.sin(angle),
                 cy + (x - cx) * math.sin(angle) + (y - cy) * math.cos(angle)) for x, y in pts]
        xs, ys = [p[0] for p in poly], [p[1] for p in poly]
        return OcrLine(text, 0.99, (min(xs), min(ys), max(xs), max(ys)), poly)

    lines = [
        rotated("1", 235, 1000, 258, 1030), rotated("Sri. A. Witness, PC-12 of Riverside PS", 290, 1000, 1050, 1030),
        rotated("Assisted to IO", 1160, 1000, 1350, 1030),
        rotated("2", 235, 1060, 258, 1090), rotated("Sri. B. Witness, PC-13 of Riverside PS", 290, 1060, 1050, 1090),
        rotated("Panch for scene", 1160, 1060, 1380, 1090),
        OcrLine("bleed", 0.99, (1500, 100, 1530, 400), [(1500, 400), (1500, 100), (1530, 100), (1530, 400)]),
    ]
    boxes, skew = boxes_from_ocr(lines, 1600, 2000)
    assert abs(skew - 3.0) < 0.2
    assert "bleed" not in {b.text for b in boxes}
    rows = {r.cells[0]: r.cells[1:] for r in layout_from_boxes(boxes, page=1).rows}
    assert rows["1"][-1] == "Assisted to IO" and rows["2"][-1] == "Panch for scene"


def test_low_confidence_specks_dropped():
    boxes = [B("Real text line here", 100, 100, 600, 130), B("'0", 1500, 300, 1520, 330, conf=0.55)]
    assert "'0" not in layout_from_boxes(boxes, page=1).text


def test_narrative_after_a_row_with_missed_serials_is_not_chained_into_it():
    # OCR found only one serial; the labels below it continue the row, but the
    # narrative that follows the "Brief facts" label must not
    boxes = [
        B("16", 373, 102, 400, 135), B("Result of Laboratory", 433, 108, 674, 150), B("analysis", 430, 151, 528, 190),
        B("Brief facts of the case", 432, 193, 688, 234),
        B("IN THE COURT OF THE MAGISTRATE", 700, 296, 1108, 334),
        B("Honoured Sir,", 355, 390, 520, 433),
        B("Facts of the case are that on 02.03.2017 the complainant reported a theft of", 427, 446, 1463, 486),
        B("his bicycle from the market and the police registered the case and investigated", 348, 490, 1462, 530),
    ]
    lay = layout_from_boxes(boxes, page=2)
    rows = {r.cells[0]: r.cells[1:] for r in lay.rows}
    assert rows["16"] == ["Result of Laboratory\nanalysis"]
    free = [t for _, t in lay.free_lines]
    assert free[0] == "Brief facts of the case" and free[-1].startswith("his bicycle")


def test_value_column_sitting_lower_than_labels_is_realigned():
    # Warped photocopy: every value sits ~0.6 of a line lower than its label (no rotation in the lines)
    from app.services.layout_service import boxes_from_ocr
    from app.services.ocr_service import OcrLine

    lines = []
    labels = ["Final Report No.", "Date", "Acts and sections", "Type of final report", "If original or supplementary",
              "Name of the complainant"]
    values = ["/2021", "02.03.2017", "379 IPC", "Charge sheet", "Original", "State through SI"]
    for i, (label, value) in enumerate(zip(labels, values)):
        y = 300 + 40 * i
        lines.append(OcrLine(f"0{i + 2}", 0.99, (240, y, 285, y + 34)))
        lines.append(OcrLine(label, 0.99, (325, y, 325 + 14 * len(label), y + 34)))
        lines.append(OcrLine(value, 0.99, (900, y + 24, 900 + 16 * len(value), y + 58)))
    boxes, _ = boxes_from_ocr(lines, 1600, 2200)
    rows = {r.cells[0]: r.cells[1:] for r in layout_from_boxes(boxes, page=1).rows}
    assert [rows[f"0{i + 2}"] for i in range(6)] == [[lab, val] for lab, val in zip(labels, values)]


def test_straight_page_is_not_shifted():
    from app.services.layout_service import column_offsets

    boxes = []
    for i in range(6):
        y = 300 + 40 * i
        boxes += [B(f"0{i + 2}", 240, y, 285, y + 34), B("Some label text", 325, y, 600, y + 34), B("value", 900, y, 1000, y + 34)]
    assert set(column_offsets(boxes, 1600, 34)) == {0.0}


def test_title_above_the_first_row_is_not_part_of_it():
    boxes = [
        B("CHARGE SHEET", 690, 157, 905, 198), B("(Under sec. 173 Cr.P.C.)", 688, 193, 1021, 240),
        B("01", 240, 212, 283, 248), B("Dist. Northtown", 322, 214, 488, 255), B("PS: RIVERSIDE", 548, 214, 813, 255),
        B("02", 241, 288, 288, 323), B("Charge sheet No.", 325, 291, 731, 330), B("/2021", 920, 291, 1001, 330),
    ]
    lay = layout_from_boxes(boxes, page=1)
    rows = {r.cells[0]: r.cells[1:] for r in lay.rows}
    assert rows["01"] == ["Dist. Northtown", "PS: RIVERSIDE"]
    assert [t for _, t in lay.free_lines][:2] == ["CHARGE SHEET", "(Under sec. 173 Cr.P.C.)"]


def test_court_heading_after_the_last_row_is_not_part_of_it():
    boxes = [
        B("16", 240, 100, 285, 134), B("Sri. A. Officer, Inspector of Police", 325, 100, 1000, 134),
        B("IO", 1200, 100, 1260, 134),
        B("IN THE HON'BLE JUDICIAL MAGISTRATE COURT", 400, 150, 1150, 184),
        B("AT NORTHTOWN.", 650, 186, 900, 220),
        B("Honoured Sir,", 330, 260, 520, 294),
    ]
    lay = layout_from_boxes(boxes, page=2)
    rows = {r.cells[0]: r.cells[1:] for r in lay.rows}
    assert rows["16"] == ["Sri. A. Officer, Inspector of Police", "IO"]
    assert [t for _, t in lay.free_lines][0].startswith("IN THE HON'BLE")


def test_reading_order_is_top_to_bottom_then_left_to_right_on_a_tilted_page():
    import math

    from app.services.layout_service import reading_order

    t = math.radians(3.0)

    def tilted(x0, y0, x1, y1):
        # positions of a page photographed 3° clockwise (right side lower)
        dy = (x0 - 200) * math.tan(t)
        return (x0, y0 + dy, x1, y1 + dy)

    boxes = [tilted(1100, 300, 1300, 334), tilted(200, 300, 500, 334), tilted(200, 345, 500, 379), tilted(1100, 345, 1300, 379)]
    assert reading_order(boxes, 3.0) == [1, 0, 2, 3]
