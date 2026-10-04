from quant_system.data.crypto_perps.current_market import fetch_current_instrument_definition
for v in ("BYBIT_LINEAR","OKX_SWAP"):
 d=fetch_current_instrument_definition(v)
 print(v,"tick",d.tick_size,"lot",d.lot_size,"contract",d.contract_value,"status",d.status,"effective",d.effective_from.isoformat(),"received",d.received_at.isoformat(),"sha",d.raw_sha256)
