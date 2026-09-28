# Docker Compose invocation: modern Docker ships the `docker compose` plugin,
# older installs ship the standalone `docker-compose` binary. Detect once.
COMPOSE := $(shell if docker compose version >/dev/null 2>&1; then echo "docker compose"; elif command -v docker-compose >/dev/null 2>&1; then echo "docker-compose"; else echo "docker-compose"; fi)

.PHONY: check env install install-local install-no-sll install-prod-domain install-db \
        generate-local-cert certificate stop start restart update logs reload-nginx \
        seed-demo-data start-http restart-nginx clear-logs

# Preflight: fail with an actionable message instead of "No such file or directory".
check:
	@command -v docker >/dev/null 2>&1 || { \
		echo "✗ Docker is not installed. See https://docs.docker.com/engine/install/"; exit 1; }
	@docker compose version >/dev/null 2>&1 || command -v docker-compose >/dev/null 2>&1 || { \
		echo "✗ Docker Compose not found (neither 'docker compose' nor 'docker-compose')."; \
		echo ""; \
		echo "  Recommended — install the Compose plugin:"; \
		echo "    Debian/Ubuntu : sudo apt-get update && sudo apt-get install -y docker-compose-plugin"; \
		echo "    RHEL/Fedora   : sudo dnf install -y docker-compose-plugin"; \
		echo "    Docs          : https://docs.docker.com/compose/install/linux/"; \
		echo ""; \
		echo "  Alternative — standalone v2 binary:"; \
		echo "    sudo curl -SL https://github.com/docker/compose/releases/latest/download/docker-compose-linux-x86_64 -o /usr/local/bin/docker-compose"; \
		echo "    sudo chmod +x /usr/local/bin/docker-compose"; \
		exit 1; }
	@echo "→ compose: $(COMPOSE)"

# First-run configuration: create .env from .env.example and replace the
# placeholder credentials with generated ones. An existing .env is never touched.
env:
	@if [ -f .env ]; then echo "→ .env present (left unchanged)"; \
	elif [ -f .env.example ]; then \
		cp .env.example .env; \
		gen() { openssl rand -hex 16 2>/dev/null || head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n'; }; \
		sed -i.bak "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$$(gen)|" .env; \
		sed -i.bak "s|^CLICKHOUSE_PASSWORD=.*|CLICKHOUSE_PASSWORD=$$(gen)|" .env; \
		sed -i.bak "s|^JWT_SECRET=.*|JWT_SECRET=$$(gen)|" .env; \
		rm -f .env.bak; \
		echo "→ created .env from .env.example with generated secrets"; \
	else \
		echo "✗ No .env and no .env.example — cannot configure the stack."; exit 1; \
	fi

install-db:
	docker exec tracker_backend pip install --no-cache-dir -r /app/install/requirements.txt
	docker exec tracker_backend python3 /app/install/install.py

install-no-sll: check env
	cp nginx/nginx.dev.conf nginx/default.conf
	$(MAKE) generate-local-cert
	$(COMPOSE) --compatibility up --build -d
	$(MAKE) install-db

install-prod-domain: check env
	cp nginx/nginx.prod.conf nginx/default.conf
	$(COMPOSE) --compatibility up --build -d
	$(MAKE) certificate
	$(MAKE) install-db

install: check env
	cp nginx/nginx.dev.conf nginx/default.conf
	$(MAKE) generate-local-cert
	$(COMPOSE) --compatibility up --build -d
	$(MAKE) install-db

install-local: check env
	cp nginx/nginx.dev.conf nginx/default.conf
	$(MAKE) generate-local-cert
	$(COMPOSE) --compatibility up --build -d
	$(MAKE) install-db

generate-local-cert:
	mkdir -p ssl
	openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
		-keyout ssl/selfsigned.key \
		-out ssl/selfsigned.crt \
		-subj "/C=US/ST=Local/L=Local/O=Dev/OU=Dev/CN=localhost"

certificate:
	docker exec tracker_nginx certbot --nginx

stop: check
	$(COMPOSE) down

start: check env
	$(COMPOSE) --compatibility up --build -d

restart: check env
	$(COMPOSE) down && $(COMPOSE) --compatibility up --build -d

update: check env
	git pull
	$(MAKE) restart

logs: check
	$(COMPOSE) logs -f

reload-nginx:
	docker exec tracker_nginx nginx -s reload

seed-demo-data:
	docker exec -it tracker_frontend python3 /app/scripts/seed_demo.py

start-http: check env
	$(COMPOSE) up -d nginx backend frontend

restart-nginx: check
	$(COMPOSE) restart nginx

clear-logs: check
	@echo "Stopping containers..."
	$(COMPOSE) down
	@echo "Truncating logs..."
	sudo truncate -s 0 /var/lib/docker/containers/*/*-json.log || true
	@echo "Clearing logs..."
	$(COMPOSE) logs --no-color > /dev/null 2>&1 || true
	@docker system prune -f --volumes || true
	@echo "Logs cleared (via prune)."
	rm -f logs/*.log || true
	@echo "Log files removed"
	@echo "Starting containers..."
	$(COMPOSE) --compatibility up --build -d