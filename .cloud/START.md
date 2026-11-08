# Inicio en el entorno cloud

Usa el checkout existente `/workspace/Acortador-de-URLs`. Cada tarea cloud está
acotada a su propio entorno; no crees un worktree salvo petición explícita.

La instantánea puede retener `.venv`, imágenes y volúmenes, pero los procesos no
sobreviven necesariamente a una nueva tarea. Desde el checkout:

```sh
docker compose up -d --wait --wait-timeout 90
curl --fail http://127.0.0.1:8000/readyz
docker compose ps
```

Deben estar sanos API, worker, PostgreSQL y Redis. Si cambió el código o las
dependencias, ejecuta antes `bash .cloud/install.sh`. Ese script usa el lockfile y
monta el CA del sistema sólo durante el build, sin desactivar la verificación TLS.
Preserva cualquier `.env` existente. Los valores incluidos son de desarrollo;
no imprimas ni guardes credenciales reales. No se requiere un token GitHub extra.

Validación desde el mismo directorio:

```sh
UV_CACHE_DIR=/workspace/.cache/uv uv run ruff check .
UV_CACHE_DIR=/workspace/.cache/uv uv run ruff format --check .
UV_CACHE_DIR=/workspace/.cache/uv uv run pytest
```

Las pruebas de integración usan Testcontainers: requieren el daemon Docker local.
Los benchmarks y sus cambios temporales de TTL/límites se documentan en README.
Este Docker rootless requiere `LOCAL_UID=0 LOCAL_GID=0` en la invocación de k6 para
leer los bind mounts; usa `--no-deps` para conservar los valores de la API preparada.
Al terminar, restaura la configuración de desarrollo con
`docker compose up -d --wait --force-recreate api`.

GeoIP es opcional. Para habilitarlo, el usuario aporta una base GeoLite2 Country
licenciada en `geoip/`, legible para el usuario del contenedor, y configura
`GEOIP_DATABASE_PATH=/geoip/GeoLite2-Country.mmdb`. Sin base, país desconocido.
No inventes credenciales ni descargues bases de licencia restringida.

Estos archivos conservan las instrucciones aunque el guardado del borrador de
configuración no esté disponible. No equivalen a publicar una instantánea.
