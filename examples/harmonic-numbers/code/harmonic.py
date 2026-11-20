"""Harmonic numbers H(n) = 1 + 1/2 + ... + 1/n, and how close H(n) - ln(n) is to the
Euler-Mascheroni constant. Writes out/results.json."""
import json
import math
import os

GAMMA = 0.5772156649015329


def harmonic(n):
    return math.fsum(1.0 / k for k in range(1, n + 1))


if __name__ == "__main__":
    n = 1_000_000
    h = harmonic(n)
    os.makedirs("out", exist_ok=True)
    result = {"n": n, "H": h, "gap": h - math.log(n) - GAMMA,
              "first_n_over_10": next(k for k in range(1, 20000) if harmonic(k) > 10)}
    with open("out/results.json", "w") as fh:
        json.dump(result, fh, indent=2)
    print(f"H({n}) = {h:.12f}")
