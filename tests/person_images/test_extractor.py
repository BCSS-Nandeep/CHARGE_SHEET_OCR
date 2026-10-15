"""The copied extraction / detection code: formats, filtering, naming, and that it never deletes anything."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

pytest.importorskip("cv2")

from integrations.person_images import extractor  # noqa: E402
from tests.person_images.helpers import BLUE, RED, make_docx, make_pdf, make_png, make_xlsx_bytes  # noqa: E402

MODEL_SHA256 = "8F2383E4DD3CFBB4553EA8718107FC0423210DC964F9F4280604804ED2552FA4"  # the original project's model


def test_the_copied_model_is_byte_identical_to_the_original():
    assert hashlib.sha256(extractor.MODEL.read_bytes()).hexdigest().upper() == MODEL_SHA256


def test_detection_thresholds_and_rotation_order_are_the_originals():
    assert (extractor.MIN_IMAGE_SIZE, extractor.SCORE_THRESHOLD, extractor.ROTATED_SCORE_THRESHOLD,
            extractor.MAX_SIDE) == (40, 0.7, 0.8, 1600)
    assert [d for d, _ in extractor.ROTATIONS] == [0, 90, 270, 180]


def test_real_detector_loads_and_finds_no_person_in_plain_images():
    """Negative control with the real YuNet model: coloured noise and a blank image contain no face."""
    for blob in (make_png(RED), make_png(BLUE, size=(300, 300), seed=3)):
        w, h, faces = extractor.detect_faces(blob)
        assert faces == 0 and (w, h) != (0, 0)


def test_real_detector_on_an_optional_real_face_image(monkeypatch):
    path = __import__("os").environ.get("IR_TEST_FACE_IMAGE")
    if not path or not Path(path).is_file():
        pytest.skip("set IR_TEST_FACE_IMAGE to a photo of a face to run the positive control")
    assert extractor.detect_faces(Path(path).read_bytes())[2] >= 1


def test_extracts_images_from_pdf_docx_and_xlsx(tmp_path):
    imgs = [make_png(RED, seed=1), make_png(BLUE, seed=2)]
    pdf = make_pdf(tmp_path / "a.pdf", imgs).read_bytes()
    docx = make_docx(tmp_path / "a.docx", imgs).read_bytes()
    xlsx = make_xlsx_bytes(imgs)
    got_pdf, got_docx, got_xlsx = extractor.extract_pdf(pdf), extractor.extract_docx(docx), extractor.extract_xlsx(xlsx)
    assert len(got_pdf) == len(got_docx) == len(got_xlsx) == 2
    assert [page for _, page, _ in got_pdf] == [1, 1]  # PDF images know their page, Office images do not
    assert all(page is None for _, page, _ in got_docx + got_xlsx)


def test_small_images_are_not_extracted_from_a_pdf(tmp_path):
    pdf = make_pdf(tmp_path / "s.pdf", [make_png(RED, size=(30, 30)), make_png(RED, size=(120, 120), seed=4)]).read_bytes()
    assert len(extractor.extract_pdf(pdf)) == 1  # the 30x30 icon is below MIN_IMAGE_SIZE


def test_only_person_images_are_saved_and_named_after_the_report(tmp_path, fake_faces):
    imgs = [make_png(BLUE, seed=1), make_png(RED, seed=2), make_png(RED, seed=3), make_png(RED, seed=4)]
    docx = make_docx(tmp_path / "My Report-12.docx", imgs).read_bytes()
    out = tmp_path / "run"
    result = extractor.save_person_images("My Report-12.docx", docx, out, base_name="My Report-12")
    assert result["total"] == 4 and len(result["saved"]) == 3 and result["unreadable"] == 0
    # first image "<name> image", then "<name> image (2)", "<name> image (3)"
    assert sorted(p.name for p in out.iterdir()) == ["My Report-12 image (2).png", "My Report-12 image (3).png",
                                                     "My Report-12 image.png"]
    assert [s["name"] for s in result["saved"]] == ["My Report-12 image.png", "My Report-12 image (2).png",
                                                    "My Report-12 image (3).png"]
    assert [s["faces"] for s in result["saved"]] == [1, 1, 1]
    for s in result["saved"]:
        assert hashlib.sha256((out / s["name"]).read_bytes()).hexdigest() == s["sha256"]


def test_without_a_base_name_images_are_called_person_image(tmp_path, fake_faces):
    docx = make_docx(tmp_path / "x.docx", [make_png(RED, seed=1)]).read_bytes()
    result = extractor.save_person_images("x.docx", docx, tmp_path / "run")
    assert result["saved"][0]["name"] == "person image.png"


def test_nothing_is_created_when_no_person_is_found(tmp_path, fake_faces):
    docx = make_docx(tmp_path / "n.docx", [make_png(BLUE)]).read_bytes()
    out = tmp_path / "run"
    result = extractor.save_person_images("n.docx", docx, out)
    assert result["saved"] == [] and result["total"] == 1 and not out.exists()


def test_an_existing_folder_with_files_is_never_overwritten_or_deleted(tmp_path, fake_faces):
    out = tmp_path / "run"
    out.mkdir()
    keep = out / "person_001.png"
    keep.write_bytes(b"previous result")
    docx = make_docx(tmp_path / "d.docx", [make_png(RED)]).read_bytes()
    with pytest.raises(FileExistsError):
        extractor.save_person_images("d.docx", docx, out)
    assert keep.read_bytes() == b"previous result"  # the original deleted the whole folder here; the copy refuses


def test_unreadable_images_are_counted_not_hidden(tmp_path, fake_faces):
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/media/image1.png", make_png(RED))
        z.writestr("word/media/image2.png", b"this is not an image")
    result = extractor.save_person_images("x.docx", buf.getvalue(), tmp_path / "run")
    assert result["total"] == 2 and result["unreadable"] == 1 and len(result["saved"]) == 1


def test_unsupported_extension_is_refused():
    with pytest.raises(ValueError):
        extractor.save_person_images("notes.txt", b"x", Path("unused"))
