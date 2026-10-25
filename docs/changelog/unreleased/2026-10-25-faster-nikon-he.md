## Performance

- **Nikon HE decodes in parallel:** the codestream's slices (each resets its prediction) are entropy-decoded in parallel, the four components' wavelets too, and the inverse Star-Tetrix colour transform is our own row-parallel version of ISO/IEC 21122-1 Annex F, bit-exact with the one it replaces: Z f HE raw decode 1009 → 127 ms, the same samples. Thanks @Kaan88. (no PR)
