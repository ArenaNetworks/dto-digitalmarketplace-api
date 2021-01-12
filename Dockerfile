FROM python:2.7-stretch

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

ADD . .

CMD FLASK_DEBUG=true FLASK_APP=wsgi.py flask run --host=0.0.0.0