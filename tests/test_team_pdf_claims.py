"""PDF reading in the Team's fetcher, and the deterministic claim-to-source check."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import httpx2 as httpx  # noqa: E402

from app.team import claimcheck as cc  # noqa: E402
from app.team import webfetch as wf  # noqa: E402
from app.team.model import Source, SourceKind  # noqa: E402

PUBLIC = "93.184.216.34"


def make_pdf(pages: list[str]) -> bytes:
    """A minimal, valid PDF with one Helvetica text line per page."""
    objs: list[bytes] = []
    n = len(pages)
    kids = " ".join(f"{3 + i * 2} 0 R" for i in range(n))
    objs.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objs.append(f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode())
    for i, text in enumerate(pages):
        content_id = 4 + i * 2
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {content_id} 0 R "
                    f"/Resources << /Font << /F1 << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> >> >> >>".encode())
        stream = f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET".encode()
        objs.append(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
    out = b"%PDF-1.4\n"
    offsets = []
    for idx, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{idx} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n".encode() + b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return out


def fetcher(body: bytes, content_type="application/pdf", **kw):
    def handler(request):
        return httpx.Response(200, headers={"content-type": content_type}, content=body)
    return wf.PageFetcher(resolver=lambda host, port: [PUBLIC],
                          transport=httpx.MockTransport(handler), **kw)


class PdfFetch(unittest.TestCase):
    def test_text_layer_is_extracted_with_page_markers(self):
        pdf = make_pdf(["Quarterly revenue was 4.2 million dollars in the northern region.",
                        "Headcount grew to 310 employees by December."])
        page = fetcher(pdf).fetch("https://example.com/report.pdf")
        self.assertEqual(page.content_type, "application/pdf")
        self.assertIn("[page 1]", page.text)
        self.assertIn("4.2 million", page.text)
        self.assertIn("310 employees", page.text)

    def test_page_count_is_bounded(self):
        pdf = make_pdf([f"Page number {i} has some filler words to be long enough here." for i in range(60)])
        page = fetcher(pdf, max_chars=500_000).fetch("https://example.com/big.pdf")
        self.assertTrue(page.truncated)
        self.assertNotIn("[page 41]", page.text)

    def test_garbage_pdf_is_a_clean_unsupported_error(self):
        with self.assertRaises(wf.FetchError) as ctx:
            fetcher(b"%PDF-1.4 this is not really a pdf at all, just words").fetch("https://example.com/x.pdf")
        self.assertIn(ctx.exception.kind, (wf.FetchErrorKind.UNSUPPORTED, wf.FetchErrorKind.EMPTY))

    def test_pdf_without_text_says_it_may_be_a_scan(self):
        with self.assertRaises(wf.FetchError) as ctx:
            fetcher(make_pdf([""])).fetch("https://example.com/scan.pdf")
        self.assertEqual(ctx.exception.kind, wf.FetchErrorKind.EMPTY)
        self.assertIn("scan", ctx.exception.message)

    def test_other_binary_types_are_still_refused(self):
        with self.assertRaises(wf.FetchError) as ctx:
            fetcher(b"PK\x03\x04", content_type="application/zip").fetch("https://example.com/a.zip")
        self.assertEqual(ctx.exception.kind, wf.FetchErrorKind.UNSUPPORTED)


def src(sid, text, kind=SourceKind.WEB, depth="page"):
    return Source(id=sid, kind=kind, title=sid, text=text, depth=depth)


class ClaimCheck(unittest.TestCase):
    SOURCES = {
        "S1": src("S1", "Widget B costs $14 and is rated for five years of daily use. "
                        "The casing is die-cast aluminium and the bearings are sealed."),
        "S2": src("S2", "Widget B is on sale for 20 dollars this week", depth="snippet"),
    }

    def check(self, text):
        return cc.check_claims(text, self.SOURCES)

    def test_supported_sentence_is_found(self):
        r = self.check("Widget B costs $14 and is rated for five years of daily use [S1].")
        self.assertEqual([x.verdict for x in r], [cc.Verdict.FOUND])

    def test_a_wrong_figure_is_flagged_even_when_the_words_match(self):
        r = self.check("Widget B costs $19 and is rated for five years of daily use [S1].")
        self.assertEqual(r[0].verdict, cc.Verdict.PARTIAL)
        self.assertEqual(r[0].missing_numbers, ["19"])

    def test_unrelated_claim_is_not_found(self):
        r = self.check("The company was founded in Lisbon by two former engineers in a garage [S1].")
        self.assertEqual(r[0].verdict, cc.Verdict.NOT_FOUND)

    def test_snippet_only_support_is_labelled_not_promoted_to_found(self):
        r = self.check("Widget B is on sale for 20 dollars this week according to the listing [S2].")
        self.assertEqual(r[0].verdict, cc.Verdict.SNIPPET)

    def test_best_of_several_citations_counts(self):
        r = self.check("Widget B costs $14 and is rated for five years of daily use [S2][S1].")
        self.assertEqual(r[0].verdict, cc.Verdict.FOUND)

    def test_source_without_text_is_reported_not_assumed_fine(self):
        r = cc.check_claims("Widget B costs $14 and is rated for five years of use [S3].",
                            {"S3": src("S3", "")})
        self.assertEqual(r[0].verdict, cc.Verdict.NO_TEXT)

    def test_unknown_citation_is_ignored_here(self):
        self.assertEqual(self.check("Something that is long enough to be a claim [S9]."), [])

    def test_possible_conflict_between_figures_on_similar_wording(self):
        results = [cc.ClaimResult("Widget B battery lasts 12 hours in testing", ["S1"], cc.Verdict.FOUND),
                   cc.ClaimResult("Widget B battery lasts 18 hours in testing", ["S2"], cc.Verdict.FOUND)]
        self.assertEqual(len(cc.possible_conflicts(results)), 1)

    def test_agreeing_figures_are_not_a_conflict(self):
        results = [cc.ClaimResult("Widget B battery lasts 12 hours in testing", ["S1"], cc.Verdict.FOUND),
                   cc.ClaimResult("The Widget B battery lasts 12 hours in lab testing", ["S2"], cc.Verdict.FOUND)]
        self.assertEqual(cc.possible_conflicts(results), [])

    def test_appendix_states_its_own_limits_and_lists_only_problems(self):
        r = self.check("Widget B costs $19 and is rated for five years of daily use [S1]. "
                       "The casing is die-cast aluminium and the bearings are sealed [S1].")
        text = cc.appendix(r)
        self.assertIn("not a fact check", text)
        self.assertIn("19", text)
        self.assertEqual(text.count("figure(s) not in the source"), 1)

    def test_uncited_figures_are_listed(self):
        out = cc.uncited_figure_sentences("Revenue grew 40% last year across all regions. Nothing numeric here at all.")
        self.assertEqual(len(out), 1)


if __name__ == "__main__":
    unittest.main()


class EngineAppendix(unittest.TestCase):
    def test_final_result_flags_a_figure_the_cited_source_does_not_contain(self):
        from tests.test_team_pages_recovery import FakeFetcher, run, script
        client = script(final=["Widget A costs $12 and ships with a long warranty included [S1]."])
        _, mission = run(client, fetcher=FakeFetcher())
        final = mission.artifact(mission.final_artifact_id).content
        self.assertIn("## Citation check (automatic)", final)
        self.assertIn("figure(s) not in the source: 12", final)
        self.assertIn("not a fact check", final)
