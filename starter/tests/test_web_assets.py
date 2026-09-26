"""静态前端：托管、离线守卫、目录缺失时的降级。

前端是零构建的（`starter/web/` 三件套，原生 JS + 手写 SVG），评审在**干净环境**
里按 README 跑起来，可能没有外网。所以这里盯三件事：

1. `GET /` 能拿到页面，`/api/*` 没被静态挂载盖住；
2. 三件套里不许出现任何远端地址或远端库（守卫写得很钝，见下）；
3. `web/` 目录不在时**导入期不能抛**（契约 §7.2：没配 Key、目录不全也要能起）。
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
ASSETS = ("index.html", "app.js", "style.css")


def test_the_three_assets_are_in_the_repo():
    """前端必须**跟着仓库走**：评审 clone 下来就该有，不能靠生成。"""
    for name in ASSETS:
        path = WEB / name
        assert path.is_file(), "web/%s 不在仓库里" % name
        assert path.stat().st_size > 0, "web/%s 是空的" % name


def test_index_is_served_at_the_root(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "调试面板" in response.text, "拿到的不是前端页面"


def test_the_api_is_not_shadowed_by_the_static_mount(client):
    """挂载点写在 `/api/*` 之后，接口不能被静态目录盖住。

    Starlette 按注册顺序匹配，`app.mount("/", ...)` 放在前面会把所有接口吃掉，
    而且是**静默**的——页面照样打得开。
    """
    for path in ("/api/health", "/api/stores", "/api/traces"):
        response = client.get(path)
        assert response.status_code == 200, "%s 被静态挂载盖住了" % path
        assert response.headers["content-type"].startswith("application/json")


def test_the_assets_reference_nothing_remote():
    """离线守卫：三件套里不许出现远端地址或远端库。

    守卫**故意写得很钝**——只找 `http://`、`https://` 和 `cdn` 三个子串，
    连注释里出现都算数。钝一点才拦得住"顺手引一个图表库"这种改动；
    想让守卫放行，请改代码而不是改这条测试。
    """
    for name in ASSETS:
        text = (WEB / name).read_text(encoding="utf-8").lower()
        for needle in ("http://", "https://", "cdn"):
            assert needle not in text, "web/%s 里出现了 %r：前端不能依赖外网" % (name, needle)


def test_the_page_has_no_build_step():
    """没有构建步骤，也就没有模块脚本：模块脚本在部分内网代理下会被 CORS 挡掉，
    而这个页面的全部意义是"clone 下来就能开"。

    这里用正则匹配**真的 `<script>` 标签**，不像上一条那样找子串：注释里
    写"不用模块脚本"是正常的，不该被绊倒。
    """
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"<script[^>]*type\s*=\s*['\"]module['\"]", html)
    assert re.search(r"<script[^>]*defer", html), "脚本应该用 defer 加载"


def _import_server_in(project_dir: Path) -> subprocess.CompletedProcess:
    """在**另一个进程**里把 `starter/` 当成项目目录导入 server。

    必须是新进程：挂不挂静态目录是 `server` 在**导入期**决定的，同进程里
    模块早就导完了，改不动那个决定。
    """
    script = (
        "import sys, pathlib\n"
        "sys.path.insert(0, %r)\n"
        "from kbqa import config\n"
        "config.PROJECT_DIR = pathlib.Path(%r)\n"
        "from kbqa import server\n"
        "names = [getattr(r, 'name', '') for r in server.app.routes]\n"
        "print('WEB-MOUNTED' if 'web' in names else 'WEB-ABSENT')\n" % (str(ROOT), str(project_dir))
    )
    return subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, cwd=str(ROOT))


def test_the_web_directory_is_actually_mounted():
    result = _import_server_in(ROOT)
    assert result.returncode == 0, result.stderr
    assert "WEB-MOUNTED" in result.stdout, "web/ 明明在，却没被挂上"


def test_a_missing_web_directory_does_not_break_the_import(tmp_path):
    """`web/` 不在时降级成 404，**不是**在导入期抛异常。

    导入期一炸，整个服务连同 `/api/*` 全起不来——契约 §7.2 要求干净环境下
    也要能启动，这里守的就是那条。
    """
    result = _import_server_in(tmp_path)  # tmp_path 下没有 web/
    assert result.returncode == 0, "web/ 不在导致导入失败：\n%s" % result.stderr
    assert "WEB-ABSENT" in result.stdout
