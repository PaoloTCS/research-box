# Rerun on GitHub Actions (beta)

The `rerun` workflow reruns a research-box drop on GitHub's own machines and signs the result,
so a reader can check that a box's declared numbers came out again on a machine its author
does not control.

## How a rerun goes

1. **fetch:** downloads the drop archive from an https address and checks its sha256. It never
   runs the box.
2. **rerun:** unpacks the drop safely and runs `build_box.py --reproduce --no-network` three
   times. Each run has no network and its own process namespace, so nothing the box starts
   outlives its run. This job holds no credentials.
3. **attest:** never runs the box. It checks the three run reports against the drop itself
   (drop id, command, declared expectations, claim, no network), works out again whether the
   declared values already appear in the box's own files, and refuses to sign if anything
   disagrees. It then signs `report.json` with GitHub's keyless artifact attestation
   (Sigstore), which records it in Sigstore's public transparency log, and publishes the
   report, the page and the signature bundle to the `certificates` branch.

A maintainer starts each rerun by hand (Actions, rerun, Run workflow). With no drop address
it reruns the example box in `examples/prime-numbers`. On pull requests the
workflow reruns the example box as a self-test and signs nothing.

## Checking a report

```
gh attestation verify report.json --repo PaoloTCS/research-box \
  --signer-workflow PaoloTCS/research-box/.github/workflows/rerun.yml
python3 build_box.py DROP --verify-report report.json --attested-by PaoloTCS/research-box
```

The second command does the first for you, then checks that the report is about this exact
drop and that the drop still matches its manifests.

## What a reader has to trust

- GitHub: that its machines ran the workflow file at the commit the attestation names.
- Sigstore: its certificate authority and public log.
- This repository: that the workflow file does what it says. It is public, so anyone can read
  the exact version that signed.

## What a report does not show

That the outputs were computed rather than stored or fetched before the run, that the method
is correct, or that the result holds on other machines. The box's own code produced what the
run printed. The report lists any declared value that already appears in the box's files.
