FROM python:3.12-slim

WORKDIR /app

# Слой зависимостей отдельно — кэшируется, пока requirements.txt не менялся.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Код. interests.md кладём как дефолт, но на сервере он bind-mount'ится
# (config-as-code: правим отбор без пересборки образа).
COPY clock.py netcfg.py collect.py filter_ai.py database.py render.py telegram.py digest.py interests.md ./

# Секреты (.env) и данные (digest.db) в образ НЕ попадают — приходят
# через env_file и bind-mount в момент запуска.
#
# Команду (collect | send) передаёт `docker compose run`:
#   docker compose run --rm digest collect
#   docker compose run --rm digest send
ENTRYPOINT ["python", "digest.py"]
