# Docker Compose invocation: modern Docker ships the `docker compose` plugin,
# older installs ship the standalone `docker-compose` binary. Detect once.
COMPOSE := $(shell if docker compose version >/dev/null 2>&1; then echo "docker compose"; elif command -v docker-compose >/dev/null 2>&1; then echo "docker-compose"; else echo "docker-compose"; fi)

# ── presentation ────────────────────────────────────────────────────────────
# Colour is switched off automatically when output is not a terminal (pipes, CI).
ifeq ($(shell [ -t 1 ] && echo tty),tty)
  B := \033[1m
  D := \033[2m
  G := \033[32m
  Y := \033[33m
  C := \033[36m
  R := \033[0m
else
  B :=
  D :=
  G :=
  Y :=
  C :=
  R :=
endif

# $(call step,text) — a single aligned progress line; keep commas out of text.
step = @printf '  $(C)$(B)▸$(R) %s\n' "$(1)"
warn = @printf '  $(Y)$(B)!$(R) %s\n' "$(1)"

SCHEME ?= http

.PHONY: banner check env preclean reset summary doctor install install-db install-db-fresh \
        install-local install-no-sll install-http install-prod-domain generate-local-cert \
        certificate stop start restart update logs reload-nginx seed-demo-data start-http \
        restart-nginx clear-logs

banner:
	@printf '\n$(B)$(C)  AAA TRACKER$(R) $(D)·$(R) $(B)setup$(R)\n'
	@printf '$(D)  ─────────────────────────────────────────────$(R)\n\n'

# Preflight: fail with an actionable message instead of "No such file or directory".
check:
	@command -v docker >/dev/null 2>&1 || { \
		printf '\n  $(Y)$(B)✗ Docker is not installed$(R)\n    https://docs.docker.com/engine/install/\n\n'; exit 1; }
	@docker compose version >/dev/null 2>&1 || command -v docker-compose >/dev/null 2>&1 || { \
		printf '\n  $(Y)$(B)✗ Docker Compose not found$(R) (neither "docker compose" nor "docker-compose")\n\n'; \
		printf '    Recommended — install the Compose plugin:\n'; \
		printf '      Debian/Ubuntu : sudo apt-get update && sudo apt-get install -y docker-compose-plugin\n'; \
		printf '      RHEL/Fedora   : sudo dnf install -y docker-compose-plugin\n'; \
		printf '      Docs          : https://docs.docker.com/compose/install/linux/\n\n'; \
		printf '    Alternative — standalone v2 binary:\n'; \
		printf '      sudo curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 -o /usr/local/bin/docker-compose\n'; \
		printf '      sudo chmod +x /usr/local/bin/docker-compose\n\n'; \
		exit 1; }
	$(call step,using $(COMPOSE))

# First-run configuration: create .env from .env.example. An existing .env is never
# touched. Placeholder credentials are replaced with generated ones ONLY when no
# database volume exists yet — a database keeps the password it was first created
# with, so rotating it out from under an existing volume would break the stack.
env:
	@if [ -f .env ]; then \
		printf '  $(C)$(B)▸$(R) configuration: .env present (left unchanged)\n'; \
	elif [ -f .env.example ]; then \
		cp .env.example .env; \
		if docker volume ls --format '{{.Name}}' | grep -qx 'aaa-tracker_postgres_data' \
		   || docker volume ls --format '{{.Name}}' | grep -qx 'postgres_data'; then \
			printf '  $(Y)$(B)!$(R) configuration: database volume found but .env was missing\n'; \
			printf '    keeping the .env.example credentials so they match that volume.\n'; \
			printf '    for fresh credentials instead: $(B)make reset && rm .env && make install$(R)\n'; \
		else \
			gen() { openssl rand -hex 16 2>/dev/null || head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n'; }; \
			sed -i.bak "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$$(gen)|" .env; \
			sed -i.bak "s|^CLICKHOUSE_PASSWORD=.*|CLICKHOUSE_PASSWORD=$$(gen)|" .env; \
			sed -i.bak "s|^JWT_SECRET=.*|JWT_SECRET=$$(gen)|" .env; \
			rm -f .env.bak; \
			printf '  $(C)$(B)▸$(R) configuration: .env created with generated secrets\n'; \
		fi; \
	else \
		printf '  $(Y)$(B)✗ No .env and no .env.example — cannot configure the stack.$(R)\n'; exit 1; \
	fi

# A previous attempt (or a deleted checkout) leaves containers holding the fixed
# names this stack uses, so `compose up` fails with "container name is already in
# use". Stop the current project, then force-remove ONLY the container names this
# compose file defines. Data lives in named volumes and is never touched here.
preclean: check
	@$(COMPOSE) down --remove-orphans >/dev/null 2>&1 || true; \
	names=$$(grep -h 'container_name:' docker-compose.yml docker-compose.prod.yml 2>/dev/null | awk '{print $$2}' | sort -u); \
	stale=""; \
	for n in $$names; do \
		if docker ps -a --format '{{.Names}}' | grep -qx "$$n"; then stale="$$stale $$n"; fi; \
	done; \
	if [ -n "$$stale" ]; then \
		printf '  $(C)$(B)▸$(R) clearing containers left over from a previous install\n'; \
		for n in $$stale; do printf '      $(D)%s$(R)\n' "$$n"; done; \
		docker rm -f $$stale >/dev/null 2>&1 || true; \
		printf '      $(D)data volumes preserved$(R)\n'; \
	fi

# Ensure the database schema exists — idempotent, never drops anything.
install-db:
	@docker exec tracker_backend pip install --no-cache-dir -q -r /app/install/requirements.txt
	docker exec tracker_backend python3 /app/install/install.py
	@printf '  $(C)$(B)▸$(R) databases ready\n'
	@# The services boot before the tables exist, so their startup migrations could not
	@# run — restart them now that the schema is in place, then prove they came back.
	@printf '  $(C)$(B)▸$(R) restarting services to apply runtime migrations\n'
	@$(COMPOSE) restart backend frontend >/dev/null 2>&1 || printf '  $(Y)$(B)!$(R) restart command failed\n'
	@if scripts/wait-for-backend.sh; then \
		printf '  $(C)$(B)▸$(R) backend is up (auth gate answering)\n'; \
	else \
		printf '  $(Y)$(B)!$(R) backend did not answer after the restart — run: make doctor\n'; \
	fi

# DESTRUCTIVE: drop every table and recreate the schema (all tracking data is lost).
install-db-fresh:
	@docker exec tracker_backend pip install --no-cache-dir -q -r /app/install/requirements.txt
	docker exec tracker_backend python3 /app/install/install.py --recreate

install-no-sll: banner check env preclean
	$(call step,nginx: dev profile (HTTP + self-signed HTTPS))
	@cp nginx/nginx.dev.conf nginx/default.conf
	$(call step,tls: generating a self-signed certificate)
	@$(MAKE) --no-print-directory generate-local-cert
	$(call step,containers: building and starting)
	@$(COMPOSE) --compatibility up --build -d --remove-orphans
	$(call step,database: ensuring schema)
	@$(MAKE) --no-print-directory install-db
	@$(MAKE) --no-print-directory summary SCHEME=http

install-http: install-no-sll

install-prod-domain: banner check env preclean
	$(call step,nginx: production profile (HTTPS for your domain))
	@cp nginx/nginx.prod.conf nginx/default.conf
	$(call step,containers: building and starting)
	@$(COMPOSE) --compatibility up --build -d --remove-orphans
	$(call step,tls: requesting a certificate with certbot)
	@$(MAKE) --no-print-directory certificate
	$(call step,database: ensuring schema)
	@$(MAKE) --no-print-directory install-db
	@$(MAKE) --no-print-directory summary SCHEME=https

install: banner check env preclean
	$(call step,nginx: dev profile (HTTP + self-signed HTTPS))
	@cp nginx/nginx.dev.conf nginx/default.conf
	$(call step,tls: generating a self-signed certificate)
	@$(MAKE) --no-print-directory generate-local-cert
	$(call step,containers: building and starting)
	@$(COMPOSE) --compatibility up --build -d --remove-orphans
	$(call step,database: ensuring schema)
	@$(MAKE) --no-print-directory install-db
	@$(MAKE) --no-print-directory summary SCHEME=http

install-local: install

generate-local-cert:
	@mkdir -p ssl
	@openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
		-keyout ssl/selfsigned.key \
		-out ssl/selfsigned.crt \
		-subj "/C=US/ST=Local/L=Local/O=Dev/OU=Dev/CN=localhost" 2>/dev/null

certificate:
	@docker exec tracker_nginx certbot --nginx

# Everything you need after a successful install.
summary:
	@ip=$$(hostname -I 2>/dev/null | awk '{print $$1}'); \
	[ -z "$$ip" ] && ip=$$(curl -s --max-time 3 https://checkip.amazonaws.com 2>/dev/null | tr -d '\n'); \
	[ -z "$$ip" ] && ip=localhost; \
	printf '\n  $(G)$(B)✓ ready$(R)\n\n'; \
	printf '  $(B)dashboard$(R)   $(SCHEME)://%s/backend\n' "$$ip"; \
	printf '  $(B)login$(R)       tracker_admin / admin   $(Y)change it right away$(R)\n'; \
	printf '  $(B)help$(R)        make logs · make restart · make update · make check\n\n'

stop: check
	@$(COMPOSE) down

start: banner check env preclean
	@$(COMPOSE) --compatibility up --build -d --remove-orphans

restart: banner check env preclean
	@$(MAKE) --no-print-directory stop
	@$(COMPOSE) --compatibility up --build -d --remove-orphans
	@printf '\n  $(G)$(B)✓ restarted$(R)\n\n'

update: banner check env preclean
	@$(call step,pulling the latest code)
	@git pull
	@$(MAKE) --no-print-directory restart

doctor: check
	@bash scripts/doctor.sh

logs: check
	@$(COMPOSE) logs -f

# DESTRUCTIVE: removes this tracker's containers AND data volumes (all clicks,
# conversions, settings). Use it to start from scratch, not to upgrade.
reset: banner check
	$(warn,removing containers AND data volumes — all tracking data is lost)
	@$(COMPOSE) down --volumes --remove-orphans >/dev/null 2>&1 || true
	@names=$$(grep -h 'container_name:' docker-compose.yml docker-compose.prod.yml 2>/dev/null | awk '{print $$2}' | sort -u); \
	for n in $$names; do docker rm -f "$$n" >/dev/null 2>&1 || true; done; \
	printf '  $(G)$(B)✓$(R) reset complete — run $(B)make install$(R) for a clean setup\n\n'

reload-nginx:
	@docker exec tracker_nginx nginx -s reload
	@printf '  $(G)$(B)✓$(R) nginx reloaded\n'

seed-demo-data:
	@docker exec -it tracker_frontend python3 /app/scripts/seed_demo.py

start-http: check env preclean
	@$(COMPOSE) up -d nginx backend frontend

restart-nginx: check
	@$(COMPOSE) restart nginx

clear-logs: check
	$(call step,stopping containers)
	@$(COMPOSE) down
	$(call step,truncating docker logs)
	@sudo truncate -s 0 /var/lib/docker/containers/*/*-json.log 2>/dev/null || true
	@docker system prune -f --volumes >/dev/null 2>&1 || true
	@rm -f logs/*.log 2>/dev/null || true
	$(call step,starting containers)
	@$(COMPOSE) --compatibility up --build -d
	@printf '\n  $(G)$(B)✓$(R) logs cleared\n\n'
