## Performance

- **Single-strip DNGs decode in parallel:** a lossless JPEG whose restart interval is whole rows is split at its restart markers and decoded on every core (bit-identical), a whole-image chunk is no longer copied sample by sample, and `GainMap` opcodes run per row in parallel: a 50 MP phone DNG's raw decode 1137 → 66 ms, preview 1.6 → 0.4 s. Thanks @Kaan88. (no PR)
