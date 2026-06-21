with open(".env", "r") as f:
    text = f.read()

text = text.replace("SENTRY_DSN=https://your-key@sentry.io/your-project-id", "SENTRY_DSN=")

with open(".env", "w") as f:
    f.write(text)
