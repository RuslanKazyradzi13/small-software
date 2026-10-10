# The small gateway (see gateway.py): Caddy for the traffic, Python's standard library for sign-in.
FROM caddy:2.11.7-alpine
RUN apk add --no-cache python3
COPY Caddyfile /etc/caddy/Caddyfile
COPY gateway.py magic_link.py /srv/
WORKDIR /srv
CMD ["python3", "-u", "/srv/gateway.py"]
