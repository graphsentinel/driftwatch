# Fallback recordings

Pre-recorded asciinema casts, one per scenario, for use if the live cluster
misbehaves on stage:

    sN.cast   (s1..s5)

Record with:

    asciinema rec -c "make demo-1" recordings/s1.cast
    ...

Play with:

    asciinema play recordings/s1.cast

These are the safety net behind the live `make demo-1..5`. The standalone demos need
only Python, so they are themselves a fallback for the in-cluster path.
