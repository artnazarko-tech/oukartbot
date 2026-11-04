#!/bin/sh
# Автообновление: забирает новую версию с GitHub и перезапускает бота, если что-то изменилось.
# Запускается cron'ом каждые 5 минут.
cd "$(dirname "$0")" || exit 1
OLD=$(git rev-parse HEAD 2>/dev/null)
git fetch -q origin main && git reset -q --hard origin/main
NEW=$(git rev-parse HEAD)
if [ "$OLD" != "$NEW" ]; then
  echo "$(date '+%F %T') обновление $OLD -> $NEW"
  docker compose up -d --build
fi
