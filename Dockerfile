FROM apify/actor-python:3.13
USER myuser
COPY --chown=myuser:myuser requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=myuser:myuser . ./
CMD ["python3", "-m", "src"]
