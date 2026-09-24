#!/bin/bash
# repetitor.db zaxira nusxasi — har kuni cron bilan ishga tushiring
# Misool: 0 3 * * * /path/to/repetitor_bot/backup.sh

set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
BACKUP_DIR="${DIR}/backups"
mkdir -p "$BACKUP_DIR"
STAMP=$(date +%Y%m%d_%H%M%S)
SRC="${DIR}/repetitor.db"
if [ -f "$SRC" ]; then
  cp "$SRC" "${BACKUP_DIR}/repetitor_${STAMP}.db"
  # 14 kundan eski zaxiralarni o'chirish
  find "$BACKUP_DIR" -name 'repetitor_*.db' -mtime +14 -delete
  echo "Backup OK: ${BACKUP_DIR}/repetitor_${STAMP}.db"
else
  echo "DB topilmadi: $SRC"
  exit 1
fi
