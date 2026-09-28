FROM nginx:alpine

# certbot for domain certificates, openssl for the self-signed fallback
RUN apk add --no-cache certbot certbot-nginx openssl

# Generates /etc/nginx/ssl/selfsigned.{crt,key} when missing or mismatched, before nginx
# starts — so the HTTPS server block cannot fail on a broken/missing pair.
COPY docker-entrypoint.d/10-selfsigned.sh /docker-entrypoint.d/10-selfsigned.sh
RUN chmod +x /docker-entrypoint.d/10-selfsigned.sh
