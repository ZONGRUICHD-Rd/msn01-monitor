#!/usr/bin/env bash
set -euo pipefail

VERSION="${1:-1.0.0}"
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DIST_DIR="${ROOT_DIR}/dist"
TOPDIR="$(mktemp -d)"
trap 'rm -rf "$TOPDIR"' EXIT

mkdir -p "${TOPDIR}"/{BUILD,RPMS,SOURCES,SPECS,SRPMS} "${DIST_DIR}"

rpmbuild -bb \
  --define "_topdir ${TOPDIR}" \
  --define "_pkg_version ${VERSION}" \
  --define "_src_root ${ROOT_DIR}" \
  --define "dist %{nil}" \
  "${ROOT_DIR}/packaging/linux/msn01-monitor.spec"

RPM_FILE="$(find "${TOPDIR}/RPMS/noarch" -name '*.rpm' | head -n 1)"
OUT_RPM="${DIST_DIR}/msn01-monitor-${VERSION}-1.noarch.rpm"
cp "${RPM_FILE}" "${OUT_RPM}"
echo "Built RedHat/yum RPM package: ${OUT_RPM}"
rpm -qip "${OUT_RPM}"
rpm -qlp "${OUT_RPM}"
