FROM python:3.10-alpine

COPY requirements.txt /
RUN pip install -r /requirements.txt

COPY . /app
WORKDIR /app

# CMD ["gunicorn", "wsgi:app"]
CMD ["python", "wsgi.py"]
