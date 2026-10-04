# EQS Interface Workstream Handoff — Read-Only v1

## Baseline

Verified parent SHA-256 before changes:
`743cba6fefe568ccaad8dd6993a615bec41e75ca4e622e5e5de0d4c345608470`

Parent:
`evolutionary-quant-system-master-r1.3-feature-f5.6-adapter-alpha-v1-nonlive-real-market-binding-enforced-v1.zip`

## Delivered

A new read-only interface layer has been added without changing execution authority or protected research evidence. The interface contains the eight requested areas and uses a GET-only read-model contract. It can be run immediately with packaged evidence and labelled mock runtime state, then wired to real runtime/status APIs behind the same adapter seam later.

Primary additions:
- `interface/index.html`
- `interface/styles.css`
- `interface/app.js`
- `interface/README.md`
- `src/quant_system/interface/read_model.py`
- `src/quant_system/interface/mock_adapter.py`
- `scripts/serve_readonly_interface.py`
- `tests/test_interface_readonly.py`
- `docs/INTERFACE_READ_ONLY_V1.md`
- interface test-evidence files.

## Safety

No UI or API path exists for placing orders, enabling LIVE, editing F7, changing risk limits, modifying preregistered campaigns, accessing credentials, or mutating immutable research evidence. HTTP write methods are rejected with 405.

The Strategies registry deliberately remains empty. Alpha-v1 remains `ALPHA_DATA_BINDING_NOT_READY`; the UI does not manufacture a winning strategy or profitability result.

## Verification

- targeted interface tests: 5 passed;
- full repository: 270 passed, 1 intentional skip;
- compileall: PASS;
- TEST_DATA guard: PASS;
- protected R1.2/F7/tracker non-cache files: 31/31 unchanged.

## Run

```bash
python scripts/serve_readonly_interface.py
```

Open `http://127.0.0.1:8765`.

## Next integration step

Implement a real `ReadOnlyEqsAdapter` that maps current runtime/status APIs into `eqs-interface-read-model-v1`. Keep command mutations out of this interface until a separately reviewed authorization design exists.
