import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.import_calibration_docx import parse_transcript


DOCUMENT_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>{body}</w:body></w:document>"""
STYLES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/></w:style>
</w:styles>"""


def paragraph(text, style=""):
    prop = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    return f"<w:p>{prop}<w:r><w:t>{text}</w:t></w:r></w:p>"


class ImportCalibrationDocxTests(unittest.TestCase):
    def test_seven_cases_preserve_paragraphs_and_isolate_labels(self):
        body = []
        for index in range(1, 8):
            body.extend(
                [
                    paragraph(f"Sample {index}", "Heading1"),
                    paragraph(f"Official band {4 + (index - 1) * 0.5}"),
                    paragraph("Source internal reference"),
                    paragraph("Task prompt", "Heading2"),
                    paragraph(f"Question {index}"),
                    paragraph("Candidate response - transcribed", "Heading2"),
                    paragraph(f"Essay paragraph one {index}."),
                    paragraph(f"Essay paragraph two {index}."),
                    paragraph("Transcription note Must not enter the essay."),
                    paragraph("Examiner comment", "Heading2"),
                    paragraph(f"Private comment {index}"),
                ]
            )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.docx"
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("word/document.xml", DOCUMENT_XML.format(body="".join(body)))
                archive.writestr("word/styles.xml", STYLES_XML)
            dataset = parse_transcript(path)

        self.assertEqual(len(dataset["cases"]), 7)
        case = dataset["cases"][0]
        self.assertEqual(case["model_input"]["candidate_response"], "Essay paragraph one 1.\n\nEssay paragraph two 1.")
        serialized_input = str(case["model_input"])
        self.assertNotIn("Official band", serialized_input)
        self.assertNotIn("Private comment", serialized_input)
        self.assertNotIn("Transcription note", serialized_input)


if __name__ == "__main__":
    unittest.main()
