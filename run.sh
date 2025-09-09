#!/bin/bash


source venv/bin/activate
uvicorn main:app --reload --host 0.0.0.0 --port 8000 &
BACKEND_PID=$!
deactivate

kill $BACKEND_PID