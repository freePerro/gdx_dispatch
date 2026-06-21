import re

path = 'gdx_dispatch/core/tenant.py'
with open(path, 'r') as f:
    text = f.read()

text = text.replace('or "gdx"', 'or "00000000-0000-0000-0000-000000000001"')
text = text.replace('slug": os.getenv("GDX_TENANT_SLUG") or "gdx"', 'slug": os.getenv("GDX_TENANT_SLUG") or "example"')

with open(path, 'w') as f:
    f.write(text)
