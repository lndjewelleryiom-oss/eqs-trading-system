from quant_system.audit.hashlog import HashChainedAuditLog


def test_hash_chain_detects_tampering(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = HashChainedAuditLog(path)
    log.append("SIGNAL", {"x": 1})
    log.append("RISK_DECISION", {"allow": False})
    assert log.verify()
    data = path.read_text().replace('"x": 1', '"x": 2')
    path.write_text(data)
    assert not HashChainedAuditLog(path).verify()
