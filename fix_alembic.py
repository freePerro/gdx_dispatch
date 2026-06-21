with open("gdx_dispatch/alembic.ini", "r") as f:
    text = f.read()

text = text.replace("sqlalchemy.url = postgresql://localhost/gdx_control", "sqlalchemy.url = postgresql://gdx:dev_password@db:5432/gdx")

with open("gdx_dispatch/alembic.ini", "w") as f:
    f.write(text)
