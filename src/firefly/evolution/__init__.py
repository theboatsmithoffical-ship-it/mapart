"""FireFly Dithering Optimization Engine — standalone, GUI-independent.

Reconstructed implementation: real dithering algorithms, deterministic
evolution loop, .ffevopack package exchange (export / inspect / merge),
inbox with quarantine + dedupe, local re-benchmarking of imported
candidates, checkpoints + resume.

No network use, no GUI access, never executes code imported from packages.
"""

VERSION = "1.0.0"
