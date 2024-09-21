FROM python:3.11-slim

RUN pip install poetry==1.2.0

COPY pyproject.toml poetry.lock ./

RUN poetry export -f requirements.txt --output requirements.txt && \
    pip install -r requirements.txt -t /asset
    
COPY . /asset
