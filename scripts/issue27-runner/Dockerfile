# Reproducible runner for container-isolated generated Python strategies (#27).
#
# The base image is pinned by digest and the two libraries are the versions
# accepted for the #26 real-Discovery Mock (numpy 2.2.6, pandas 2.2.3).
# Nothing else is installed.  Build with scripts/issue27-runner/build.sh; the
# executor runs this image with --network none, so the image must be complete.
FROM python:3.12-bookworm@sha256:dbbe4ceb97851e2e5fa83798b239811f871cb743b259ba3563737349f6bcfaa0

RUN pip install --no-cache-dir "numpy==2.2.6" "pandas==2.2.3"
