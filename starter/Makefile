# 经营看板 + 问答服务
# 换数据或知识库：make rebuild DATA_DIR=/path/data KB_DIR=/path/kb
#
# 评测三件套（都对着**已经起好的服务**跑，服务用 make run 起）：
#   make eval-free   不调模型的三类，22 分，免费，随时可跑
#   make eval-mock   完整 55 题，自己起一个"没配 Key"的服务，免费
#   make eval        完整 55 题，对着 live 服务跑，要花钱，人工触发
# 三者跑完都会自动跟回归基线比一次，掉分则退出码非零。

PY ?= .venv/bin/python
# 评测脚本是纯标准库的，用系统 python3 就行
PY3 ?= python3
PORT ?= 8000
# eval-mock 自己起的那个服务的端口，别跟 run 撞。
#
# **注释不要写在赋值行末尾**：GNU Make 会把注释**前面**的空格一起留在变量值里
# （`MOCK_PORT = 8010               `），拼进 URL 就是个连不上的地址。
MOCK_PORT ?= 8010

REPO ?= $(abspath $(CURDIR)/..)
EVAL_DIR ?= $(REPO)/eval
SCRIPTS ?= $(REPO)/scripts
REPORTS ?= $(EVAL_DIR)/reports
#: 回归基线：拿到满分的那一轮。换题库/换数据之后要另存一份再指过来。
EVAL_BASE ?= $(REPORTS)/2026-09-26_confirm_100
PUBLIC ?= $(EVAL_DIR)/public_questions.jsonl
MINE ?= $(EVAL_DIR)/my_questions.jsonl
STAMP ?= $(shell date +%Y-%m-%d_%H%M)

export DATA_DIR KB_DIR VAR_DIR LLM_BASE_URL LLM_API_KEY LLM_MODEL

.PHONY: setup rebuild run test clean eval-free eval-mock eval eval-mine

setup:          ## 建虚拟环境并装依赖
	uv venv --python 3.12
	uv pip install --python $(PY) -r requirements.txt

rebuild:        ## 从 DATA_DIR 与 KB_DIR 重建清洗表和检索索引
	$(PY) -m kbqa.rebuild

run:            ## 启动服务，默认 8000 端口
	$(PY) -m uvicorn kbqa.server:app --host 127.0.0.1 --port $(PORT)

test:           ## 跑测试
	$(PY) -m pytest tests -q

clean:          ## 删掉清洗表
	rm -rf var

eval-free:      ## 不调模型的三类评测（22 分，免费）
	@set -e; \
	for cat in retrieval metrics health; do \
	  out=$(REPORTS)/$(STAMP)_free_$$cat; \
	  echo "==== $$cat ===="; \
	  $(PY3) $(EVAL_DIR)/run_eval.py --base-url http://127.0.0.1:$(PORT) \
	      --questions $(PUBLIC) --only $$cat --out $$out; \
	  $(PY3) $(SCRIPTS)/compare_eval.py $(EVAL_BASE) $$out; \
	  echo; \
	done; \
	echo "不调模型的三类都跟基线比过了。"

eval-mock:      ## 完整 55 题，自己起一个没配 Key 的服务来跑（免费）
	@set -e; \
	pid=""; \
	cleanup() { [ -n "$$pid" ] && kill $$pid 2>/dev/null; true; }; \
	trap cleanup EXIT; \
	echo "起一个没配 Key 的服务在 $(MOCK_PORT) 端口……"; \
	env -u LLM_BASE_URL -u LLM_API_KEY -u LLM_MODEL \
	    $(PY) -m uvicorn kbqa.server:app --host 127.0.0.1 --port $(MOCK_PORT) \
	    >/dev/null 2>&1 & \
	pid=$$!; \
	ready=""; \
	for _ in $$(seq 1 60); do \
	  if curl -sf http://127.0.0.1:$(MOCK_PORT)/api/health >/dev/null; then ready=yes; break; fi; \
	  sleep 0.5; \
	done; \
	if [ -z "$$ready" ]; then echo "服务没起来（端口 $(MOCK_PORT) 被占了？）"; exit 1; fi; \
	out=$(REPORTS)/$(STAMP)_mock; \
	$(PY3) $(EVAL_DIR)/run_eval.py --base-url http://127.0.0.1:$(MOCK_PORT) \
	    --questions $(PUBLIC) --out $$out; \
	$(PY3) $(SCRIPTS)/compare_eval.py $(EVAL_BASE) $$out

eval:           ## 完整 55 题，对着 live 服务跑（要 Key、要花钱，人工触发）
	@mode=$$(curl -sf http://127.0.0.1:$(PORT)/api/health \
	    | $(PY3) -c "import json,sys; print(json.load(sys.stdin).get('llm_mode','?'))" \
	    2>/dev/null || echo "连不上"); \
	if [ "$$mode" != "live" ]; then \
	  echo "服务不是 live 模式（当前：$$mode），这一趟会白跑或者白花钱。"; \
	  echo "  先按 README 配好 Key 并 make run；不花钱的走 make eval-mock。"; \
	  exit 1; \
	fi
	@set -e; \
	out=$(REPORTS)/$(STAMP)_live; \
	$(PY3) $(EVAL_DIR)/run_eval.py --base-url http://127.0.0.1:$(PORT) \
	    --questions $(PUBLIC) --out $$out; \
	$(PY3) $(SCRIPTS)/compare_eval.py $(EVAL_BASE) $$out

eval-mine:      ## 跑自己的那 8 道题（先离线体检，再连着服务跑）
	@$(PY3) $(SCRIPTS)/check_my_questions.py
	@set -e; \
	out=$(REPORTS)/$(STAMP)_mine; \
	$(PY3) $(EVAL_DIR)/run_eval.py --base-url http://127.0.0.1:$(PORT) \
	    --questions $(MINE) --out $$out; \
	echo "报告在 $$out（自己的题没有历史基线，所以不跟谁比）"
