FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SESSION_STATE_FILE=/data/session_state.json \
    FYERS_TOKEN_FILE=/data/fyers_access_token

WORKDIR /app

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data

COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --upgrade pip \
    && grep -v '^fyers-apiv3==' requirements.txt > /tmp/requirements.txt \
    && grep '^fyers-apiv3==' requirements.txt > /tmp/fyers-requirements.txt \
    && python -m pip install --no-cache-dir -r /tmp/requirements.txt \
    && python -m pip install --no-cache-dir --no-deps -r /tmp/fyers-requirements.txt \
    && sed -i \
        -e "s/from pkg_resources import resource_filename/from importlib.resources import files/" \
        -e "s/resource_filename('fyers_apiv3.FyersWebsocket', 'map.json')/str(files('fyers_apiv3.FyersWebsocket').joinpath('map.json'))/" \
        /usr/local/lib/python3.13/site-packages/fyers_apiv3/FyersWebsocket/data_ws.py \
    && grep -q "from importlib.resources import files" \
        /usr/local/lib/python3.13/site-packages/fyers_apiv3/FyersWebsocket/data_ws.py \
    && ! grep -q "pkg_resources" \
        /usr/local/lib/python3.13/site-packages/fyers_apiv3/FyersWebsocket/data_ws.py

COPY --chown=app:app . .
RUN chown app:app /data

USER app

EXPOSE 8080

CMD ["python", "nifty_multi_strategy_master.py"]
