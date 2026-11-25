#!/bin/bash
# Patch nginx to serve the SEO Meta Manager HTML tool as a static file,
# bypassing the React SPA router.
#
# Run on EC2 via SSM:
#   aws s3 cp s3://nexabuilder-root-site-979841141166-us-west-1-an/deploy/patch_nginx_seo_manager.sh /tmp/patch_nginx_seo_manager.sh
#   bash /tmp/patch_nginx_seo_manager.sh
set -e

NGINX_CONF="/etc/nginx/sites-available/nexabuilder-backend.conf"
HTML_SRC="/var/www/nexabuilder/admin-console/seo-meta-manager.html"
HTML_DEST="/var/www/nexabuilder/frontend/admin-console/seo-meta-manager.html"

echo "=== Step 1: Copy HTML to admin-console frontend dir ==="
cp "$HTML_SRC" "$HTML_DEST"
chmod 644 "$HTML_DEST"
echo "Copied to $HTML_DEST"

echo "=== Step 2: Check if seo-meta-manager location already exists ==="
if grep -q "seo-meta-manager" "$NGINX_CONF"; then
  echo "Location block already exists, skipping nginx patch"
else
  echo "Adding nginx location block..."
  python3 - <<'PYEOF'
import re

with open('/etc/nginx/sites-available/nexabuilder-backend.conf', 'r') as f:
    content = f.read()

new_block = """    # SEO Meta Manager — static tool, served directly (bypasses SPA router)
    location = /seo-meta-manager {
        alias /var/www/nexabuilder/frontend/admin-console/seo-meta-manager.html;
        add_header Content-Type "text/html";
        add_header Cache-Control "no-store, no-cache, must-revalidate";
    }
    location = /seo-meta-manager.html {
        alias /var/www/nexabuilder/frontend/admin-console/seo-meta-manager.html;
        add_header Content-Type "text/html";
        add_header Cache-Control "no-store, no-cache, must-revalidate";
    }
"""

target = '    location /assets/ {\n        alias /var/www/nexabuilder/frontend/admin-console/assets/;\n    }'
replacement = target + '\n' + new_block

if target in content:
    content = content.replace(target, replacement, 1)
    with open('/etc/nginx/sites-available/nexabuilder-backend.conf', 'w') as f:
        f.write(content)
    print("Nginx config patched successfully")
else:
    print("ERROR: target pattern not found in nginx config — manual edit needed")
    exit(1)
PYEOF
fi

echo "=== Step 3: Test nginx config ==="
nginx -t

echo "=== Step 4: Reload nginx ==="
systemctl reload nginx
echo "Done! https://admin.nexabuilder.com/seo-meta-manager should now load"
