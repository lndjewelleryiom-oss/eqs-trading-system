from quant_system.ci.test_data_evidence_guard import _file_is_test_data

def test_marker_after_large_unbroken_line_and_chunk_boundary(tmp_path):
    p = tmp_path / "artifact.log"
    p.write_text("x" * 200000 + "\n" + " " * 131072 + "TEST_DATA" + " " * 131072 + ": true\n", encoding="utf-8")
    assert _file_is_test_data(p)

def test_marker_does_not_accept_true_prefix_split_at_chunk_end(tmp_path):
    p = tmp_path / "artifact.log"
    p.write_text(" " * (65536 - len("TEST_DATA: true")) + "TEST_DATA: truex", encoding="utf-8")
    assert not _file_is_test_data(p)

def test_invalid_utf8_remains_nontext_even_after_marker(tmp_path):
    p = tmp_path / "artifact.bin"
    p.write_bytes(b"TEST_DATA: true\n" + b" " * 100000 + b"\xff")
    assert not _file_is_test_data(p)

def test_final_line_marker_without_newline(tmp_path):
    p = tmp_path / "artifact.log"
    p.write_text("TEST DATA = yes", encoding="utf-8")
    assert _file_is_test_data(p)
