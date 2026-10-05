"""Shared test helpers."""
import zlib

import pytest


@pytest.fixture
def make_pdf():
    """Write a one-page PDF the way Word and PowerPoint export them: the page text sits in a
    compressed stream, so grep can't see it. text=None draws a shape instead, like a scan."""
    def make(path, text):
        ops = (b"BT /F1 12 Tf 72 712 Td (%s) Tj ET" % text.encode() if text
               else b"0.5 g 72 600 200 100 re f")
        body = zlib.compress(ops)
        objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
                b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
                b"/Resources << /Font << /F1 5 0 R >> >> >>",
                b"<< /Length %d /Filter /FlateDecode >>\nstream\n%s\nendstream" % (len(body), body),
                b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
        out, offsets = bytearray(b"%PDF-1.4\n"), []
        for i, obj in enumerate(objs, 1):
            offsets.append(len(out))
            out += b"%d 0 obj\n%s\nendobj\n" % (i, obj)
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
        out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
        out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
        path.write_bytes(bytes(out))
    return make
