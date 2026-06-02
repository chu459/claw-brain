"""
claw-brain CLI
==============
Autonomous money-making system command-line interface.

Usage:
    python cli.py doctor
    python cli.py cred list
    python cli.py start --goal "在Gumroad上架Notion模板" --max-loops 50
"""

import os
import sys
import signal
import threading
import time
from pathlib import Path

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent))

import typer
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.prompt import Prompt, Confirm
from rich.text import Text
from rich.status import Status
from rich.live import Live
from rich.layout import Layout
from rich.rule import Rule
from rich import box

from core import (
    SystemState, RunLoopConfig, run_loop as core_run_loop,
    build_cred_summary, try_save_user_input, run_health_check,
)
from credential_store import (
    list_accounts, get_account, add_account, update_account,
    delete_account, ACCOUNT_TEMPLATES, PRESET_FIELDS,
)

app = typer.Typer(
    name="claw-brain",
    help="Claw-brain: autonomous money-making system",
    no_args_is_help=True,
    rich_markup_mode="rich",
)
console = Console()

# Global references for in-process state
_state: SystemState = SystemState()
_thread: threading.Thread = None


def _load_config() -> dict:
    """Load configuration from .env and credential store."""
    config = {}
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                config[key.strip()] = value.strip().strip("\"'")

    # Credential store fallback
    from credential_store import get_credential_value
    config.setdefault("BRAIN_API_KEY",
        get_credential_value("DeepSeek", "api_key") or "")
    config.setdefault("BRAIN_BASE_URL",
        get_credential_value("DeepSeek", "base_url") or "https://api.deepseek.com/v1")
    config.setdefault("BRAIN_MODEL",
        get_credential_value("DeepSeek", "model") or "deepseek-chat")
    config.setdefault("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789")
    config.setdefault("OPENCLAW_NODE_DIR", "")

    return config


# ===================== doctor =====================

@app.command()
def doctor():
    """Run system health checks."""
    console.print(Rule("[bold]claw-brain doctor[/bold]"))
    cfg = _load_config()

    with console.status("[bold cyan]Running health checks..."):
        results = run_health_check(
            brain_api_key=cfg.get("BRAIN_API_KEY", ""),
            brain_base_url=cfg.get("BRAIN_BASE_URL", ""),
            brain_model=cfg.get("BRAIN_MODEL", ""),
            gateway_url=cfg.get("OPENCLAW_GATEWAY_URL", ""),
        )

    table = Table(show_header=True, header_style="bold magenta", box=box.SIMPLE)
    table.add_column("Check", style="bold")
    table.add_column("Status")
    table.add_column("Detail", max_width=50)

    all_ok = True
    for name, passed, msg in results:
        icon = "[bold green]OK[/bold green]" if passed else "[bold red]FAIL[/bold red]"
        if not passed:
            all_ok = False
        table.add_row(name, icon, msg)

    console.print()
    console.print(table)
    console.print()

    if all_ok:
        console.print("[bold green]All checks passed. System is ready.[/bold green]")
    else:
        console.print("[bold yellow]Some checks failed. See details above.[/bold yellow]")


# ===================== cred =====================

@app.command()
def cred(
    action: str = typer.Argument(..., help="list | add | get | delete"),
    name: str = typer.Option("", "--name", "-n", help="Account name"),
    category: str = typer.Option("custom", "--category", "-c", help="Account category"),
    account_id: str = typer.Option("", "--id", help="Account ID (for get/delete)"),
    field_key: str = typer.Option("", "--field", "-f", help="Field key (for add)"),
    value: str = typer.Option("", "--value", "-v", help="Field value (for add)"),
):
    """Manage stored credentials."""
    if action == "list":
        accounts = list_accounts(mask=True)
        if not accounts:
            console.print("[dim]No accounts stored yet.[/dim]")
            return

        table = Table(show_header=True, header_style="bold magenta", box=box.SIMPLE)
        table.add_column("Name", style="bold")
        table.add_column("Category")
        table.add_column("Fields")

        for acc in accounts:
            cat = acc.get("category", "custom")
            tpl = ACCOUNT_TEMPLATES.get(cat, {"label": cat, "icon": "?"})
            cat_label = f"{tpl.get('icon', '?')} {tpl.get('label', cat)}"
            fields = "  ".join(
                f"{f['label']}:{f['value']}" if f.get("has_value") else f"[dim]{f['label']}:(empty)[/dim]"
                for f in acc.get("fields", [])
            )
            table.add_row(acc["name"], cat_label, fields)

        console.print(table)

    elif action == "add":
        if not name:
            name = Prompt.ask("[bold cyan]Account name[/]")

        # Show category options
        console.print("\n[bold]Select a category:[/bold]")
        for i, (key, tpl) in enumerate(ACCOUNT_TEMPLATES.items(), 1):
            console.print(f"  {i}. {tpl['icon']} {tpl['label']} ({key})")
        console.print(f"  {len(ACCOUNT_TEMPLATES)+1}. Custom")

        cat_input = Prompt.ask("[bold cyan]Category number or name[/]")
        try:
            idx = int(cat_input) - 1
            if 0 <= idx < len(ACCOUNT_TEMPLATES):
                category = list(ACCOUNT_TEMPLATES.keys())[idx]
            else:
                category = "custom"
        except ValueError:
            category = cat_input.lower().strip()

        # Determine fields based on category template
        tpl = ACCOUNT_TEMPLATES.get(category, ACCOUNT_TEMPLATES["custom"])
        fields = []
        console.print(f"\n[bold]Enter fields for {name}:[/bold] (press Enter to skip)")
        for field_def in tpl.get("fields", []):
            val = Prompt.ask(
                f"  [cyan]{field_def['label']}[/]",
                default="",
            )
            if val:
                fields.append({
                    "key": field_def["key"],
                    "label": field_def["label"],
                    "value": val,
                    "type": field_def.get("type", "text"),
                })

        # Option to add more preset fields
        if Confirm.ask("\nAdd more fields?", default=False):
            console.print("[bold]Available presets:[/bold]")
            preset_keys = [p["key"] for p in PRESET_FIELDS]
            existing_keys = {f["key"] for f in fields}
            for p in PRESET_FIELDS:
                if p["key"] not in existing_keys:
                    console.print(f"  - {p['label']} ({p['key']})")
            while True:
                add_input = Prompt.ask("[bold cyan]Field name/key (or Enter to finish)[/]", default="")
                if not add_input:
                    break
                matched = None
                for p in PRESET_FIELDS:
                    if p["key"] == add_input.lower().strip() or p["label"] == add_input.strip():
                        matched = p
                        break
                if not matched:
                    matched = {"key": add_input.lower().strip(), "label": add_input.strip(), "type": "text"}
                val = Prompt.ask(f"  [cyan]{matched['label']}[/]", default="")
                if val:
                    fields.append({
                        "key": matched["key"],
                        "label": matched["label"],
                        "value": val,
                        "type": matched.get("type", "text"),
                    })

        if not fields:
            console.print("[yellow]No fields provided. Nothing saved.[/yellow]")
            return

        result = add_account(name, category, fields)
        console.print(f"[bold green]Account '{name}' saved successfully (id: {result['id']})[/bold green]")

    elif action == "get":
        if not account_id:
            console.print("[red]--id is required for get action[/red]")
            raise typer.Exit(1)
        acc = get_account(account_id)
        if not acc:
            console.print(f"[red]Account '{account_id}' not found[/red]")
            raise typer.Exit(1)
        console.print(Panel(
            f"[bold]Name:[/bold] {acc['name']}\n"
            f"[bold]Category:[/bold] {acc['category']}\n"
            f"[bold]Created:[/bold] {acc.get('created_at', '?')}\n\n"
            + "\n".join(
                f"  [bold]{f['label']}[/bold]: {f.get('value', '(empty)')}"
                for f in acc.get("fields", [])
            ),
            title=f"Account {account_id}",
        ))

    elif action == "delete":
        if not account_id:
            console.print("[red]--id is required for delete action[/red]")
            raise typer.Exit(1)
        acc = get_account(account_id)
        if not acc:
            console.print(f"[red]Account '{account_id}' not found[/red]")
            raise typer.Exit(1)
        if Confirm.ask(f"Delete account '{acc['name']}' ({account_id})?", default=False):
            delete_account(account_id)
            console.print(f"[bold green]Deleted.[/bold green]")
        else:
            console.print("Cancelled.")

    else:
        console.print(f"[red]Unknown action: {action}[/red]")
        console.print("Valid actions: list, add, get, delete")
        raise typer.Exit(1)


# ===================== status =====================

@app.command()
def status():
    """Show current system status."""
    cfg = _load_config()

    table = Table(show_header=True, header_style="bold magenta", box=box.SIMPLE)
    table.add_column("Property", style="bold")
    table.add_column("Value")

    table.add_row("Brain API", "[green]configured[/green]" if cfg.get("BRAIN_API_KEY") else "[red]missing[/red]")
    table.add_row("Brain Model", cfg.get("BRAIN_MODEL", "deepseek-chat"))
    table.add_row("Gateway", cfg.get("OPENCLAW_GATEWAY_URL", "not set"))
    table.add_row("Running", "[green]Yes[/green]" if _state.running else "[dim]No[/dim]")
    table.add_row("Round", str(_state.loop_count))
    table.add_row("Brain Log", f"{len(_state.brain_log)} entries")
    table.add_row("Claw Log", f"{len(_state.claw_log)} entries")

    console.print(table)

    # Show recent brain log
    if _state.brain_log:
        console.print(Rule("[bold]Recent Brain Decisions[/bold]"))
        for entry in _state.brain_log[-5:]:
            st = entry.get("status", "?")
            thought = (entry.get("thought") or "")[:80]
            style = {
                "continue": "green", "milestone": "yellow",
                "blocked": "red", "need_input": "cyan", "pause": "blue",
            }.get(st, "dim")
            console.print(f"  [R{entry.get('round','?')}] [{st}] {Text(thought, style=style)}")

    # Show recent claw log
    if _state.claw_log:
        console.print(Rule("[bold]Recent OpenClaw Actions[/bold]"))
        for entry in _state.claw_log[-3:]:
            icon = "[green]+[/green]" if entry.get("success") else "[red]x[/red]"
            instr = (entry.get("instruction") or "")[:60]
            console.print(f"  {icon} R{entry.get('round','?')}: {instr}")


# ===================== start =====================

@app.command()
def start(
    goal: str = typer.Option(..., "--goal", "-g", help="Ultimate goal for the system"),
    agent: str = typer.Option("main", "--agent", "-a", help="OpenClaw agent to use"),
    max_loops: int = typer.Option(50, "--max-loops", "-m", help="Maximum loop rounds"),
    interval: int = typer.Option(15, "--interval", "-i", help="Seconds between rounds"),
):
    """Start the autonomous system."""
    global _state, _thread

    if _state.running:
        console.print("[yellow]System is already running. Use 'stop' first.[/yellow]")
        raise typer.Exit(1)

    cfg = _load_config()
    if not cfg.get("BRAIN_API_KEY"):
        console.print("[red]BRAIN_API_KEY not configured. Run 'doctor' to check.[/red]")
        raise typer.Exit(1)

    # Health check
    with console.status("[bold cyan]Checking prerequisites..."):
        results = run_health_check(
            brain_api_key=cfg["BRAIN_API_KEY"],
            brain_base_url=cfg["BRAIN_BASE_URL"],
            gateway_url=cfg["OPENCLAW_GATEWAY_URL"],
        )
    failed = [r for r in results if not r[1]]
    if failed:
        console.print("[red]Health check failed:[/red]")
        for name, _, msg in failed:
            console.print(f"  [red]x[/red] {name}: {msg}")
        console.print("\nFix the issues above or run [bold]doctor[/bold] for details.")
        raise typer.Exit(1)

    console.print(Panel(
        f"[bold]Goal:[/bold] {goal}\n"
        f"[bold]Agent:[/bold] {agent}\n"
        f"[bold]Max Loops:[/bold] {max_loops}\n"
        f"[bold]Interval:[/bold] {interval}s",
        title="[bold]Starting claw-brain[/bold]",
        border_style="green",
    ))

    _state = SystemState()
    config = RunLoopConfig(
        goal=goal, agent=agent, max_loops=max_loops, interval=interval,
        brain_api_key=cfg["BRAIN_API_KEY"],
        brain_base_url=cfg.get("BRAIN_BASE_URL", "https://api.deepseek.com/v1"),
        brain_model=cfg.get("BRAIN_MODEL", "deepseek-chat"),
        gateway_url=cfg.get("OPENCLAW_GATEWAY_URL", "http://127.0.0.1:18789"),
        session_key="autonomous-money-maker",
        memory_file=str(Path(__file__).parent / "system_memory.json"),
    )

    def on_input_needed(question: str) -> str:
        """Prompt user in terminal when Brain needs input."""
        console.print()
        console.print(Panel(question, title="[bold yellow]Brain needs your input[/bold yellow]", border_style="yellow"))
        answer = Prompt.ask("[bold cyan]Your answer[/]")
        try_save_user_input(answer, question)
        return answer

    def on_event(event_type: str, msg: str):
        """Print status events."""
        console.print(f"  [dim]{msg}[/dim]")

    # Handle Ctrl+C
    original_sigint = signal.getsignal(signal.SIGINT)

    def handle_sigint(signum, frame):
        with _state.lock:
            if _state.running:
                console.print("\n[bold yellow]Stopping... (Ctrl+C again to force quit)[/bold yellow]")
                _state.running = False
            else:
                raise KeyboardInterrupt

    signal.signal(signal.SIGINT, handle_sigint)

    # Start in background thread
    _thread = threading.Thread(
        target=core_run_loop,
        args=(_state, config, on_input_needed, on_event),
        daemon=True,
    )
    _thread.start()

    # Monitor loop in foreground
    try:
        while _state.running or _thread.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        with _state.lock:
            _state.running = False
        console.print("[yellow]Force stopping...[/yellow]")
        _thread.join(timeout=5)

    signal.signal(signal.SIGINT, original_sigint)
    console.print(f"\n[bold]System stopped. Total rounds: {_state.loop_count}[/bold]")


# ===================== stop =====================

@app.command()
def stop():
    """Stop the running system."""
    global _state
    with _state.lock:
        if not _state.running:
            console.print("[dim]System is not running.[/dim]")
            return
        _state.running = False
    console.print("[bold yellow]Stop signal sent. Waiting for clean shutdown...[/bold yellow]")
    time.sleep(2)
    console.print("[bold green]Stopped.[/bold green]")


# ===================== web =====================

@app.command()
def web(
    host: str = typer.Option("127.0.0.1", "--host", help="Bind address"),
    port: int = typer.Option(7860, "--port", "-p", help="Bind port"),
):
    """Start the web console."""
    console.print(f"[bold]Starting web console at http://{host}:{port}[/bold]")
    import uvicorn
    from web_console import app as web_app
    uvicorn.run(web_app, host=host, port=port, log_level="warning")


# ===================== xianyu (闲鱼服务) =====================

@app.command()
def xianyu(
    action: str = typer.Argument("guide", help="Action: guide / order / stats / list"),
    service_type: str = typer.Option("ppt", "--type", "-t", help="Service type: ppt/writing/design/resume"),
    requirement: str = typer.Option("", "--req", "-r", help="Customer requirement text"),
    price: float = typer.Option(0, "--price", "-p", help="Order price"),
):
    """Xianyu AI service: manage orders, generate content, track revenue."""
    from xianyu_service import (
        XianyuOrderManager, SERVICE_CONFIGS,
        print_listing_guide,
    )

    if action == "guide":
        print_listing_guide()
        return

    if action == "stats":
        manager = XianyuOrderManager()
        stats = manager.get_stats()
        table = Table(title="Xianyu Order Stats")
        table.add_column("Metric", style="bold")
        table.add_column("Value")
        table.add_row("Total Orders", str(stats["total"]))
        table.add_row("Revenue", f"[green]{stats['revenue']:.0f} CNY[/green]")
        table.add_row("Pending", str(stats["pending"]))
        table.add_row("Delivered", str(stats["delivered"]))
        console.print(table)
        return

    if action == "list":
        manager = XianyuOrderManager()
        orders = manager.list_orders()
        if not orders:
            console.print("[dim]No orders yet. Use 'xianyu order' to create one.[/dim]")
            return
        table = Table(title="Xianyu Orders")
        table.add_column("ID", style="bold")
        table.add_column("Type")
        table.add_column("Requirement", max_width=40)
        table.add_column("Price")
        table.add_column("Status")
        for o in orders[-10:]:  # Show last 10
            status_color = "green" if o["status"] in ("delivered", "paid") else "yellow" if o["status"] == "pending" else "red"
            table.add_row(
                o["id"],
                o["service_type"],
                o["requirement"][:40],
                f"{o['price']:.0f} CNY",
                f"[{status_color}]{o['status']}[/{status_color}]",
            )
        console.print(table)
        return

    if action == "order":
        if not requirement:
            console.print("[yellow]Available service types:[/yellow]")
            for key, cfg in SERVICE_CONFIGS.items():
                console.print(f"  [bold]{key}[/bold] - {cfg['name']} ({cfg['price_range']})")
            requirement = Prompt.ask("[bold cyan]Customer requirement[/]")

        cfg = SERVICE_CONFIGS.get(service_type, SERVICE_CONFIGS["ppt"])
        actual_price = price or cfg["default_price"]
        console.print(Panel(
            f"[bold]Service:[/bold] {cfg['name']}\n"
            f"[bold]Requirement:[/bold] {requirement[:100]}\n"
            f"[bold]Price:[/bold] {actual_price} CNY",
            title="[bold]Creating Xianyu Order[/bold]",
            border_style="green",
        ))

        if not Confirm.ask("Generate now?", default=True):
            console.print("[dim]Cancelled.[/dim]")
            return

        with console.status("[bold cyan]Generating content with AI..."):
            from xianyu_service import quick_create_order
            result = quick_create_order(service_type, requirement, actual_price)

        order = result["order"]
        gen = result["result"]
        if gen["success"]:
            console.print(f"[bold green]Generated successfully![/bold green]")
            console.print(f"[bold]File:[/bold] {gen['file_path']}")
            console.print(f"\n[dim]Content preview:[/dim]")
            console.print(gen["content"][:500])
        else:
            console.print(f"[bold red]Generation failed:[/red] {gen['content']}")

        return

    if action == "verify":
        """Verify/promote memories to long-term trusted knowledge."""
        from vector_memory import get_vector_memory
        vm = get_vector_memory()
        stats = vm.get_stats()

        if requirement:
            # 手动验证特定记忆
            success = vm.verify_memory(requirement, verified=True)
            if success:
                console.print(f"[bold green]Memory marked as verified.[/bold green]")
            else:
                console.print(f"[red]Memory not found. Add it first, then verify.[/red]")
        else:
            # 自动提升
            console.print(f"[bold]Auto-promoting verified memories...[/bold]")
            console.print(f"Current memories: {stats['total']}")
            count = vm.auto_promote_verified(min_occurrences=3)
            if count > 0:
                console.print(f"[bold green]Promoted {count} memories to verified.[/bold green]")
            else:
                console.print("[dim]No memories eligible for promotion yet.[/dim]")
                console.print("[dim]Keep running the system - patterns that succeed 3+ times auto-promote.[/dim]")
        return

    if action == "auto-list":
        """Auto-list products on Xianyu via OpenClaw browser automation."""
        console.print(Panel(
            "[bold]闲鱼全自动上架[/bold]\n"
            "系统将通过 OpenClaw 浏览器自动化登录闲鱼并发布AI服务商品。\n"
            "请确保:\n"
            "  1. OpenClaw Gateway 运行中 (openclaw gateway run --force)\n"
            "  2. 凭据库中已有闲鱼账号 (python cli.py cred add --name 闲鱼账号)",
            title="[bold]Auto-List Mode[/bold]",
            border_style="cyan",
        ))

        if not Confirm.ask("开始自动上架?", default=True):
            console.print("[dim]Cancelled.[/dim]")
            return

        from xianyu_auto_list import auto_publish_all
        with console.status("[bold cyan]Auto-listing on Xianyu via OpenClaw..."):
            results = auto_publish_all()

        console.print()
        table = Table(title="Auto-List Results")
        table.add_column("Type", style="bold")
        table.add_column("Title", max_width=40)
        table.add_column("Status")
        for r in results:
            icon = "[green]OK[/green]" if r["success"] else "[red]FAIL[/red]"
            table.add_row(r["template"], r["title"][:40], icon)
        console.print(table)
        return

    if action == "auto-check":
        """Check Xianyu for new orders/messages via OpenClaw."""
        console.print("[bold cyan]Checking Xianyu for new orders...[/bold cyan]")
        from xianyu_auto_list import check_new_orders
        with console.status("[bold cyan]Checking via OpenClaw..."):
            result = check_new_orders()

        console.print(Panel(result["content"][:500], title="Xianyu Status"))
        return

    if action == "auto-login":
        """Auto-login to Xianyu via OpenClaw."""
        console.print("[bold cyan]Auto-login to Xianyu...[/bold cyan]")
        from xianyu_auto_list import auto_login
        with console.status("[bold cyan]Logging in via OpenClaw..."):
            result = auto_login()

        if result["success"]:
            console.print("[bold green]Login instruction sent to OpenClaw.[/bold green]")
            console.print(result["content"][:300])
        else:
            console.print(f"[red]Login failed: {result['content']}[/red]")
        return

    console.print(f"[red]Unknown action: {action}. Use guide/order/stats/list.[/red]")


# ===================== migrate =====================

@app.command()
def migrate():
    """Migrate system_memory.json to vector memory."""
    from vector_memory import get_vector_memory
    console.print("[bold]Migrating system_memory.json to vector memory...[/bold]")
    vm = get_vector_memory()
    count = vm.migrate_from_json(str(Path(__file__).parent / "system_memory.json"))
    console.print(f"[bold green]Migrated {count} memories.[/bold green]")
    stats = vm.get_stats()
    console.print(f"Total vector memories: {stats['total']}")


# ===================== Entry Point =====================

if __name__ == "__main__":
    app()
