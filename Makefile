.PHONY: setup setup-python setup-frontend doctor dev-backend dev-frontend test build run

setup: setup-python setup-frontend

setup-python:
	python3 -m venv .venv
	PIP_CACHE_DIR=.cache/pip .venv/bin/python -m pip install --prefer-binary --timeout 60 --retries 8 -r backend/requirements.txt

setup-frontend:
	@for path in frontend/node_modules/react frontend/node_modules/react-dom frontend/node_modules/echarts frontend/node_modules/vite frontend/node_modules/@vitejs/plugin-react frontend/node_modules/.bin/vite; do \
		if [ -L "$$path" ]; then echo "removing temporary link $$path"; rm -f "$$path"; fi; \
	done
	cd frontend && npm install --prefer-offline --cache ../.cache/npm --fetch-retries=5 --fetch-retry-mintimeout=1000 --fetch-retry-maxtimeout=20000 --no-audit --no-fund

doctor:
	@python3 --version
	@node --version
	@npm --version
	@.venv/bin/python -c "import fastapi, uvicorn, requests; print('backend dependencies: OK')"
	@node -e "for (const name of ['react','react-dom','echarts','vite']) require.resolve(name,{paths:['frontend']}); console.log('frontend dependencies: OK')"

dev-backend:
	.venv/bin/uvicorn app.main:app --reload --app-dir backend --host 127.0.0.1 --port 8000

dev-frontend:
	cd frontend && npm run dev

test:
	PYTHONPATH=backend .venv/bin/python -m unittest discover -s backend/tests -p 'test_*.py'

build:
	cd frontend && npm run build

run: build
	.venv/bin/uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8000
