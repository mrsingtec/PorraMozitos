import os
from datetime import datetime, timedelta
from functools import wraps

import pytz
from flask import (Flask, flash, redirect, render_template, request,
                   session, url_for)
from flask_login import (LoginManager, UserMixin, current_user, login_required,
                          login_user, logout_user)
from werkzeug.security import check_password_hash, generate_password_hash
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "cambia-esta-clave-en-produccion")

SPAIN_TZ = pytz.timezone("Europe/Madrid")

DATABASE_URL = os.environ.get("DATABASE_URL")
DATABASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "porra.db")
USE_POSTGRES = DATABASE_URL is not None

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras
else:
    import sqlite3

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = "login"


class Database:
    def __init__(self):
        if USE_POSTGRES:
            self.conn = psycopg2.connect(DATABASE_URL)
        else:
            self.conn = sqlite3.connect(DATABASE)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA foreign_keys=ON")

    def execute(self, sql, params=None):
        if USE_POSTGRES:
            cur = self.conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql.replace("?", "%s"), params or ())
        else:
            cur = self.conn.cursor()
            if params is None:
                cur.execute(sql)
            else:
                cur.execute(sql, params)
        return cur

    def executescript(self, sql):
        if USE_POSTGRES:
            for stmt in sql.replace("\n", " ").split(";"):
                stmt = stmt.strip()
                if stmt:
                    self.execute(stmt)
        else:
            self.conn.executescript(sql)

    def commit(self):
        self.conn.commit()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is None:
            self.commit()
        self.close()


def get_db():
    return Database()


def init_db():
    with get_db() as db:
        if USE_POSTGRES:
            db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    is_admin INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (NOW())
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS matches (
                    id SERIAL PRIMARY KEY,
                    round TEXT NOT NULL,
                    home_team TEXT NOT NULL,
                    away_team TEXT NOT NULL,
                    match_date TEXT NOT NULL,
                    deadline TEXT NOT NULL,
                    home_score INTEGER,
                    away_score INTEGER,
                    status TEXT DEFAULT 'pending',
                    group_name TEXT,
                    matchday INTEGER,
                    duration TEXT NOT NULL DEFAULT 'REGULAR'
                )
            """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS predictions (
                    id SERIAL PRIMARY KEY,
                    user_id INTEGER NOT NULL,
                    match_id INTEGER NOT NULL,
                    prediction TEXT NOT NULL,
                    created_at TEXT DEFAULT (NOW()),
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (match_id) REFERENCES matches(id),
                    UNIQUE(user_id, match_id)
                )
            """)
        else:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    is_admin INTEGER DEFAULT 0,
                    created_at TEXT DEFAULT (datetime('now'))
                );
                CREATE TABLE IF NOT EXISTS matches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    round TEXT NOT NULL,
                    home_team TEXT NOT NULL,
                    away_team TEXT NOT NULL,
                    match_date TEXT NOT NULL,
                    deadline TEXT NOT NULL,
                    home_score INTEGER,
                    away_score INTEGER,
                    status TEXT DEFAULT 'pending',
                    group_name TEXT,
                    matchday INTEGER,
                    duration TEXT NOT NULL DEFAULT 'REGULAR'
                );
                CREATE TABLE IF NOT EXISTS predictions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    match_id INTEGER NOT NULL,
                    prediction TEXT NOT NULL,
                    created_at TEXT DEFAULT (datetime('now')),
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (match_id) REFERENCES matches(id),
                    UNIQUE(user_id, match_id)
                );
            """)

        # Migrate existing DB – add columns if missing
        if USE_POSTGRES:
            cols = [r["column_name"] for r in db.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'matches'"
            )]
        else:
            cols = [r["name"] for r in db.execute("PRAGMA table_info(matches)")]
        if "group_name" not in cols:
            db.execute("ALTER TABLE matches ADD COLUMN group_name TEXT")
        if "matchday" not in cols:
            db.execute("ALTER TABLE matches ADD COLUMN matchday INTEGER")
        if "duration" not in cols:
            db.execute("ALTER TABLE matches ADD COLUMN duration TEXT NOT NULL DEFAULT 'REGULAR'")

        if USE_POSTGRES:
            ucols = [r["column_name"] for r in db.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'users'"
            )]
        else:
            ucols = [r["name"] for r in db.execute("PRAGMA table_info(users)")]
        if "bonus_points" not in ucols:
            db.execute("ALTER TABLE users ADD COLUMN bonus_points INTEGER DEFAULT 0")


class User(UserMixin):
    def __init__(self, row):
        self.id = row["id"]
        self.username = row["username"]
        self.is_admin = bool(row["is_admin"])

    def get_id(self):
        return str(self.id)


@login_manager.user_loader
def load_user(user_id):
    with get_db() as db:
        row = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return User(row) if row else None


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash("Acceso no autorizado", "danger")
            return redirect(url_for("index"))
        return f(*args, **kwargs)
    return decorated


def now_spain():
    return datetime.now(SPAIN_TZ)


def parse_date(date_str):
    return datetime.strptime(date_str, "%Y-%m-%d %H:%M")


def get_unlock_time(day_str):
    """Matches of a day unlock at 23:30 the previous day (Spanish time)."""
    day_dt = parse_date(day_str + " 00:00")
    unlock = day_dt - timedelta(days=1) + timedelta(hours=23, minutes=30)
    return SPAIN_TZ.localize(unlock)


def get_close_time(match_date):
    """A match closes 30 min before kickoff (Spanish time)."""
    return SPAIN_TZ.localize(parse_date(match_date)) - timedelta(minutes=30)


def match_result(home_score, away_score):
    if home_score is None or away_score is None:
        return None
    if home_score > away_score:
        return "1"
    if home_score == away_score:
        return "X"
    return "2"


def match_result_90(match_row):
    """Return result (1/X/2) based on 90-minute regulation only.
    For knockout matches that went to extra time/penalties, result is 'X' (draw)."""
    hs = match_row["home_score"]
    aw = match_row["away_score"]
    if hs is None or aw is None:
        return None
    if match_row["round"] in KNOCKOUT_ROUNDS and match_row["duration"] != "REGULAR":
        return "X"
    return match_result(hs, aw)


"""
Traducción de rondas
"""
ROUNDS_ORDER = [
    "grupo",
    "dieciseisavos",
    "octavos",
    "cuartos",
    "semifinal",
    "tercer_puesto",
    "final",
]
ROUNDS_LABEL = {
    "grupo": "Fase de grupos",
    "dieciseisavos": "Dieciseisavos de final",
    "octavos": "Octavos de final",
    "cuartos": "Cuartos de final",
    "semifinal": "Semifinal",
    "tercer_puesto": "Tercer puesto",
    "final": "Final",
}

KNOCKOUT_ROUNDS = {"octavos", "cuartos", "semifinal", "final", "tercer_puesto", "dieciseisavos"}


# ──────────────────────────── API Config ──────────────────────────────

FOOTBALL_API_KEY = os.environ.get("FOOTBALL_API_KEY", "")
FOOTBALL_API_URL = "https://api.football-data.org/v4/competitions/{}/matches"

# football-data.org stage → ronda PorraMozitos
STAGE_MAP = {
    "GROUP_STAGE": "grupo",
    "LAST_32": "dieciseisavos",
    "LAST_16": "octavos",
    "QUARTER_FINALS": "cuartos",
    "SEMI_FINALS": "semifinal",
    "THIRD_PLACE": "tercer_puesto",
    "FINAL": "final",
}


def import_matches_from_api(competition_id="2000"):
    if not FOOTBALL_API_KEY:
        return False, "No hay API key configurada. Añade FOOTBALL_API_KEY en las variables de entorno."

    import requests

    headers = {"X-Auth-Token": FOOTBALL_API_KEY}
    url = FOOTBALL_API_URL.format(competition_id)

    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        return False, f"Error al conectar con la API: {e}"

    matches = data.get("matches", [])
    if not matches:
        return False, "No se encontraron partidos para esta competición."

    added = 0
    updated = 0

    with get_db() as db:
        for m in matches:
            stage = m.get("stage", "")
            round_name = STAGE_MAP.get(stage, stage.lower())

            home_team = TEAM_TRANSLATIONS.get((m.get("homeTeam") or {}).get("name", ""), (m.get("homeTeam") or {}).get("name", ""))
            away_team = TEAM_TRANSLATIONS.get((m.get("awayTeam") or {}).get("name", ""), (m.get("awayTeam") or {}).get("name", ""))

            if not home_team or not away_team:
                continue

            utc_date = m.get("utcDate", "")
            if not utc_date:
                continue
            utc_dt = datetime.strptime(utc_date[:16], "%Y-%m-%dT%H:%M").replace(tzinfo=pytz.UTC)
            match_date = utc_dt.astimezone(SPAIN_TZ).strftime("%Y-%m-%d %H:%M")

            # Deadline computed dynamically (see get_day_deadline); store placeholder
            deadline = match_date

            # Group info
            group_name = m.get("group") or ""
            matchday = m.get("matchday")

            # Result from API
            score = m.get("score") or {}
            ft = score.get("fullTime") or {}
            home_score = ft.get("home")
            away_score = ft.get("away")
            duration = score.get("duration", "REGULAR")

            status = "played" if home_score is not None else "pending"

            # Check for existing match by home_team, away_team and round (unique per tournament stage)
            existing = db.execute(
                "SELECT id, home_team, away_team, home_score, away_score, status, duration FROM matches WHERE home_team = ? AND away_team = ? AND round = ?",
                (home_team, away_team, round_name),
            ).fetchone()
            if existing:
                # UPDATE existing match: results, teams, round info
                # Only update scores if API has them (don't overwrite manual results with null)
                final_home = home_score if home_score is not None else existing["home_score"]
                final_away = away_score if away_score is not None else existing["away_score"]
                final_status = status if home_score is not None else existing["status"]
                final_duration = duration if home_score is not None else existing["duration"]
                db.execute(
                    """UPDATE matches
                       SET round = ?, home_team = ?, away_team = ?,
                           home_score = ?, away_score = ?, status = ?,
                           group_name = ?, matchday = ?, duration = ?
                       WHERE id = ?""",
                    (round_name, home_team, away_team,
                     final_home, final_away, final_status,
                     group_name, matchday, final_duration, existing["id"]),
                )
                updated += 1
            else:
                db.execute(
                    """INSERT INTO matches (round, home_team, away_team, match_date, deadline, home_score, away_score, status, group_name, matchday, duration)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (round_name, home_team, away_team, match_date, deadline, home_score, away_score, status, group_name, matchday, duration),
                )
                added += 1

        db.commit()

    parts = []
    if added:
        parts.append(f"{added} nuevos")
    if updated:
        parts.append(f"{updated} actualizados")
    msg = "Importación completada: " + ", ".join(parts) if parts else "Sin cambios"
    return True, msg


# ──────────────────────────────── Auth ────────────────────────────────


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        if not username or not password:
            flash("Usuario y contraseña obligatorios", "danger")
            return render_template("register.html")

        with get_db() as db:
            existing = db.execute(
                "SELECT id FROM users WHERE username = ?", (username,)
            ).fetchone()
            if existing:
                flash("Ese usuario ya existe", "danger")
                return render_template("register.html")

            is_admin = 0
            count = db.execute("SELECT COUNT(*) as cnt FROM users").fetchone()["cnt"]
            if count == 0:
                is_admin = 1

            db.execute(
                "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
                (username, generate_password_hash(password), is_admin),
            )
            db.commit()

        flash("Registro completado. Ahora inicia sesión.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        with get_db() as db:
            row = db.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()

        if row and check_password_hash(row["password_hash"], password):
            login_user(User(row))
            next_page = request.args.get("next")
            return redirect(next_page or url_for("index"))

        flash("Usuario o contraseña incorrectos", "danger")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login"))


# ────────────────────────────── Dashboard ──────────────────────────────


def compute_group_standings():
    """Compute standings for each group from match results."""
    with get_db() as db:
        matches = db.execute(
            "SELECT * FROM matches WHERE round = 'grupo' AND status = 'played'"
        ).fetchall()

    groups = {}
    for m in matches:
        g = m["group_name"] or "DESCONOCIDO"
        if g not in groups:
            groups[g] = {}
        for team in (m["home_team"], m["away_team"]):
            if team not in groups[g]:
                groups[g][team] = {"pld": 0, "w": 0, "d": 0, "l": 0, "gf": 0, "ga": 0, "pts": 0}

        ht, at = m["home_team"], m["away_team"]
        hs, as_ = m["home_score"], m["away_score"]

        groups[g][ht]["pld"] += 1
        groups[g][at]["pld"] += 1
        groups[g][ht]["gf"] += hs
        groups[g][ht]["ga"] += as_
        groups[g][at]["gf"] += as_
        groups[g][at]["ga"] += hs

        if hs > as_:
            groups[g][ht]["w"] += 1
            groups[g][ht]["pts"] += 3
            groups[g][at]["l"] += 1
        elif hs == as_:
            groups[g][ht]["d"] += 1
            groups[g][at]["d"] += 1
            groups[g][ht]["pts"] += 1
            groups[g][at]["pts"] += 1
        else:
            groups[g][at]["w"] += 1
            groups[g][at]["pts"] += 3
            groups[g][ht]["l"] += 1

    result = {}
    for g, teams in groups.items():
        sorted_teams = sorted(teams.items(), key=lambda t: (t[1]["pts"], t[1]["gf"] - t[1]["ga"], t[1]["gf"]), reverse=True)
        result[g] = [{"team": team, **stats, "gd": stats["gf"] - stats["ga"]} for team, stats in sorted_teams]

    return result


TEAM_TRANSLATIONS = {
    "Mexico": "México",
    "South Africa": "Sudáfrica",
    "South Korea": "Corea del Sur",
    "Czechia": "Chequia",
    "Canada": "Canadá",
    "Bosnia-Herzegovina": "Bosnia",
    "Qatar": "Catar",
    "Switzerland": "Suiza",
    "Brazil": "Brasil",
    "Haiti": "Haití",
    "Morocco": "Marruecos",
    "Scotland": "Escocia",
    "Australia": "Australia",
    "Paraguay": "Paraguay",
    "Turkey": "Turquía",
    "United States": "EE.UU.",
    "Curaçao": "Curazao",
    "Ecuador": "Ecuador",
    "Germany": "Alemania",
    "Ivory Coast": "Costa de Marfil",
    "Japan": "Japón",
    "Netherlands": "Países Bajos",
    "Sweden": "Suecia",
    "Tunisia": "Túnez",
    "Belgium": "Bélgica",
    "Egypt": "Egipto",
    "Iran": "Irán",
    "New Zealand": "Nueva Zelanda",
    "Cape Verde Islands": "Cabo Verde",
    "Saudi Arabia": "Arabia Saudí",
    "Spain": "España",
    "Uruguay": "Uruguay",
    "France": "Francia",
    "Iraq": "Irak",
    "Norway": "Noruega",
    "Senegal": "Senegal",
    "Algeria": "Argelia",
    "Argentina": "Argentina",
    "Austria": "Austria",
    "Jordan": "Jordania",
    "Colombia": "Colombia",
    "Congo DR": "R. D. Congo",
    "Portugal": "Portugal",
    "Uzbekistan": "Uzbekistán",
    "Croatia": "Croacia",
    "England": "Inglaterra",
    "Ghana": "Ghana",
    "Panama": "Panamá",
}


def get_open_date():
    """Return the first date whose unlock_time (23:30 prev day) hasn't passed."""
    with get_db() as db:
        dates = db.execute(
            "SELECT DISTINCT substr(match_date,1,10) as d FROM matches ORDER BY d"
        ).fetchall()

    if not dates:
        return None

    now = now_spain()

    # Check which days have all matches played (early unlock)
    with get_db() as db:
        fully_played = set()
        for dr in dates:
            pend = db.execute(
                "SELECT COUNT(*) as c FROM matches WHERE substr(match_date,1,10)=? AND status='pending'",
                (dr["d"],)
            ).fetchone()["c"]
            if pend == 0:
                fully_played.add(dr["d"])

    for i, d in enumerate(dates):
        unlock = get_unlock_time(d["d"])
        # Early unlock if all previous dates are fully played
        prev_days = [dates[j]["d"] for j in range(i)]
        prev_all_played = all(pd in fully_played for pd in prev_days) if prev_days else False
        if now < unlock and not prev_all_played:
            return d["d"]

    return dates[-1]["d"]


@app.route("/")
@login_required
def index():
    now = now_spain()
    today_str = now.strftime("%Y-%m-%d")

    with get_db() as db:
        rows = db.execute(
            "SELECT * FROM matches ORDER BY match_date ASC"
        ).fetchall()

        predictions = {
            row["match_id"]: row["prediction"]
            for row in db.execute(
                "SELECT match_id, prediction FROM predictions WHERE user_id = ?",
                (current_user.id,),
            ).fetchall()
        }

    # Pre-compute which days have all matches played (early unlock)
    with get_db() as db:
        all_day_rows = db.execute(
            "SELECT DISTINCT substr(match_date,1,10) as d FROM matches ORDER BY d"
        ).fetchall()
        days_fully_played = set()
        for dr in all_day_rows:
            pend = db.execute(
                "SELECT COUNT(*) as c FROM matches WHERE substr(match_date,1,10)=? AND status='pending'",
                (dr["d"],)
            ).fetchone()["c"]
            if pend == 0:
                days_fully_played.add(dr["d"])

    matches_by_day = {}
    for m in rows:
        match_day = m["match_date"][:10]
        unlock_time = get_unlock_time(match_day)
        close_time = get_close_time(m["match_date"])

        # Unlocked if time passed OR all previous days fully played
        prev_days = [d for d in days_fully_played | {match_day} if d < match_day]
        prev_all_played = all(d in days_fully_played for d in prev_days) if prev_days else False
        is_blocked = (now < unlock_time) and not prev_all_played
        is_open = (m["status"] == "pending") and not is_blocked and (now < close_time)

        match_dict = dict(m)
        match_dict["is_open"] = is_open
        match_dict["is_blocked"] = is_blocked
        match_dict["unlock_time"] = unlock_time.isoformat()
        match_dict["close_time"] = close_time.isoformat()
        match_dict["actual"] = match_result(m["home_score"], m["away_score"])

        if match_day not in matches_by_day:
            matches_by_day[match_day] = {}
        r = m["round"]
        if r not in matches_by_day[match_day]:
            matches_by_day[match_day][r] = []
        matches_by_day[match_day][r].append(match_dict)

    sorted_days = sorted(matches_by_day.keys())

    # Determine which tab to show as active
    active_day = None
    if today_str in matches_by_day:
        active_day = today_str
    elif sorted_days:
        active_day = sorted_days[0]

    return render_template(
        "index.html",
        matches_by_day=matches_by_day,
        sorted_days=sorted_days,
        active_day=active_day,
        predictions=predictions,
        match_result=match_result,
        ROUNDS_LABEL=ROUNDS_LABEL,
        ROUNDS_ORDER=ROUNDS_ORDER,
    )


@app.route("/grupos")
@login_required
def group_standings():
    standings = compute_group_standings()
    return render_template("groups.html", standings=standings)


def _do_predict(match_id, prediction):
    if prediction not in ("1", "X", "2"):
        return {"success": False, "message": "Pronóstico inválido"}

    now = now_spain()
    with get_db() as db:
        match = db.execute(
            "SELECT * FROM matches WHERE id = ?", (match_id,)
        ).fetchone()

        if not match:
            return {"success": False, "message": "Partido no encontrado"}

        day = match["match_date"][:10]
        unlock_time = get_unlock_time(day)
        close_time = get_close_time(match["match_date"])

        # Early unlock if all previous days' matches are fully played
        prev_pending = db.execute(
            "SELECT 1 FROM matches WHERE substr(match_date,1,10) < ? AND status='pending' LIMIT 1",
            (day,)
        ).fetchone()
        is_blocked = (now < unlock_time) and (prev_pending is not None)
        if is_blocked:
            return {"success": False, "message": "Aún no está disponible"}
        if now > close_time:
            return {"success": False, "message": "No has tenido tiempo llevando Inditex palante... ⏰"}

        db.execute(
            """INSERT INTO predictions (user_id, match_id, prediction)
               VALUES (?, ?, ?)
               ON CONFLICT(user_id, match_id) DO UPDATE SET prediction = ?""",
            (current_user.id, match_id, prediction, prediction),
        )
        db.commit()

    return {"success": True, "message": "✅ Guardado"}


@app.route("/predict/<int:match_id>", methods=["POST"])
@login_required
def predict(match_id):
    prediction = request.form.get("prediction")
    result = _do_predict(match_id, prediction)
    if result["success"]:
        flash(result["message"], "success")
    else:
        flash(result["message"], "warning")
    return redirect(url_for("index"))


@app.route("/api/predict/<int:match_id>", methods=["POST"])
@login_required
def api_predict(match_id):
    prediction = request.form.get("prediction")
    return _do_predict(match_id, prediction)


# ──────────────────────────── Leaderboard ─────────────────────────────


@app.route("/clasificacion")
@login_required
def leaderboard():
    with get_db() as db:
        users = db.execute(
            "SELECT id, username, COALESCE(bonus_points,0) as bonus_points FROM users ORDER BY username ASC"
        ).fetchall()

        matches = db.execute(
            "SELECT * FROM matches WHERE status = 'played'"
        ).fetchall()

        played_ids = [m["id"] for m in matches]
        match_data = {}
        for m in matches:
            actual = match_result_90(m)
            pts = 2 if m["round"] in KNOCKOUT_ROUNDS else 1
            match_data[m["id"]] = {"actual": actual, "pts": pts}

    standings = []
    for user in users:
        bonus = user["bonus_points"]
        if not played_ids:
            standings.append({"username": user["username"], "points": bonus, "correct": 0, "total": 0, "bonus": bonus})
            continue

        with get_db() as db:
            placeholders = ",".join("?" for _ in played_ids)
            preds = db.execute(
                f"SELECT match_id, prediction FROM predictions WHERE user_id = ? AND match_id IN ({placeholders})",
                (user["id"], *played_ids),
            ).fetchall()

        points = bonus
        correct_count = 0
        for p in preds:
            md = match_data.get(p["match_id"])
            if md and md["actual"] and p["prediction"] == md["actual"]:
                points += md["pts"]
                correct_count += 1

        standings.append({
            "username": user["username"],
            "points": points,
            "correct": correct_count,
            "total": len(played_ids),
            "bonus": bonus,
        })

    standings.sort(key=lambda x: x["points"], reverse=True)

    return render_template("leaderboard.html", standings=standings)


# ─────────────────────────────── Admin ────────────────────────────────


@app.route("/admin")
@login_required
@admin_required
def admin_panel():
    with get_db() as db:
        matches = db.execute(
            "SELECT * FROM matches ORDER BY match_date ASC"
        ).fetchall()
        users = db.execute(
            "SELECT id, username, is_admin, COALESCE(bonus_points,0) as bonus_points FROM users ORDER BY username ASC"
        ).fetchall()

    now = now_spain()
    return render_template(
        "admin.html",
        matches=matches,
        users=users,
        now=now,
        match_result=match_result,
        ROUNDS_LABEL=ROUNDS_LABEL,
    )


@app.route("/admin/predictions")
@login_required
@admin_required
def admin_predictions():
    with get_db() as db:
        users = db.execute(
            "SELECT id, username, COALESCE(bonus_points,0) as bonus_points FROM users ORDER BY username ASC"
        ).fetchall()

        matches = db.execute(
            "SELECT * FROM matches ORDER BY match_date ASC, id ASC"
        ).fetchall()

        predictions = db.execute(
            """SELECT p.user_id, p.match_id, p.prediction
               FROM predictions p
               ORDER BY p.user_id, p.match_id"""
        ).fetchall()

    pred_map = {}
    for p in predictions:
        pred_map[(p["user_id"], p["match_id"])] = p["prediction"]

    # Group matches by round for display
    matches_by_round = {}
    for m in matches:
        r = m["round"]
        if r not in matches_by_round:
            matches_by_round[r] = []
        matches_by_round[r].append(dict(m))

    # Compute correct predictions per user and per match (using 90-min rule)
    user_correct = {}
    for u in users:
        correct = 0
        for m in matches:
            actual = match_result_90(m)
            p = pred_map.get((u["id"], m["id"]))
            if actual and p == actual:
                correct += 1
        user_correct[u["id"]] = correct

    # Compute correct predictions per match (how many users got it right)
    match_correct = {}
    for m in matches:
        count = 0
        actual = match_result_90(m)
        if actual:
            for u in users:
                p = pred_map.get((u["id"], m["id"]))
                if p == actual:
                    count += 1
        match_correct[m["id"]] = count

    return render_template(
        "admin_predictions.html",
        users=users,
        matches_by_round=matches_by_round,
        ROUNDS_LABEL=ROUNDS_LABEL,
        ROUNDS_ORDER=ROUNDS_ORDER,
        pred_map=pred_map,
        match_result_90=match_result_90,
        user_correct=user_correct,
        match_correct=match_correct,
    )


@app.route("/admin/user-bonus", methods=["POST"])
@login_required
@admin_required
def set_user_bonus():
    user_id = request.form.get("user_id")
    bonus = request.form.get("bonus_points", "0")

    try:
        bonus = int(bonus)
    except ValueError:
        bonus = 0

    with get_db() as db:
        db.execute("UPDATE users SET bonus_points = ? WHERE id = ?", (bonus, user_id))
        db.commit()

    flash("Puntos actualizados ✅", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/toggle-admin/<int:user_id>", methods=["POST"])
@login_required
@admin_required
def toggle_admin(user_id):
    if user_id == current_user.id:
        flash("No puedes cambiarte los permisos a ti mismo", "warning")
        return redirect(url_for("admin_panel"))

    with get_db() as db:
        user = db.execute("SELECT is_admin FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            flash("Usuario no encontrado", "danger")
            return redirect(url_for("admin_panel"))
        new_status = 0 if user["is_admin"] else 1
        db.execute("UPDATE users SET is_admin = ? WHERE id = ?", (new_status, user_id))
        db.commit()

    flash("Permisos de admin actualizados ✅", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/matches/<int:match_id>/result", methods=["POST"])
@login_required
@admin_required
def set_result(match_id):
    home_score = request.form.get("home_score")
    away_score = request.form.get("away_score")

    if home_score == "" or away_score == "" or home_score is None or away_score is None:
        flash("Introduce el resultado", "danger")
        return redirect(url_for("admin_panel"))

    with get_db() as db:
        db.execute(
            "UPDATE matches SET home_score = ?, away_score = ?, status = 'played' WHERE id = ?",
            (int(home_score), int(away_score), match_id),
        )
        db.commit()

    flash("Resultado actualizado ✅", "success")
    return redirect(url_for("admin_panel"))


@app.route("/api/admin/matches/<int:match_id>/result", methods=["POST"])
@login_required
@admin_required
def api_set_result(match_id):
    home_score = request.form.get("home_score")
    away_score = request.form.get("away_score")

    if home_score == "" or away_score == "" or home_score is None or away_score is None:
        return {"success": False, "message": "Introduce el resultado"}

    with get_db() as db:
        db.execute(
            "UPDATE matches SET home_score = ?, away_score = ?, status = 'played' WHERE id = ?",
            (int(home_score), int(away_score), match_id),
        )
        db.commit()

    return {"success": True, "message": "Resultado actualizado ✅"}


@app.route("/api/refresh-results", methods=["POST"])
@login_required
def api_refresh_results():
    if not FOOTBALL_API_KEY:
        return {"success": False, "message": "No hay API key configurada"}
    success, msg = import_matches_from_api("2000")
    return {"success": success, "message": msg}


@app.route("/admin/matches/<int:match_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_match(match_id):
    with get_db() as db:
        db.execute("DELETE FROM predictions WHERE match_id = ?", (match_id,))
        db.execute("DELETE FROM matches WHERE id = ?", (match_id,))
        db.commit()

    flash("Partido eliminado", "success")
    return redirect(url_for("admin_panel"))


@app.route("/admin/import-matches", methods=["POST"])
@login_required
@admin_required
def admin_import_matches():
    competition_id = request.form.get("competition_id", "2002")
    success, msg = import_matches_from_api(competition_id)
    flash(msg, "success" if success else "danger")
    return redirect(url_for("admin_panel"))


# ────────────────────────────────── Init ────────────────────────────────


@app.route("/admin/init", methods=["GET", "POST"])
def admin_init():
    with get_db() as db:
        count = db.execute("SELECT COUNT(*) as cnt FROM users").fetchone()["cnt"]

    if count > 0:
        flash("Ya hay usuarios registrados", "warning")
        return redirect(url_for("login"))

    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"]

        if not username or not password:
            flash("Rellena todos los campos", "danger")
            return render_template("admin_init.html")

        with get_db() as db:
            db.execute(
                "INSERT INTO users (username, password_hash, is_admin) VALUES (?, ?, ?)",
                (username, generate_password_hash(password), 1),
            )
            db.commit()

        flash("Admin creado. Ahora inicia sesión.", "success")
        return redirect(url_for("login"))

    return render_template("admin_init.html")


@app.route("/admin/reset-db")
def reset_db():
    token = request.args.get("token", "")
    if token != "reset123":
        flash("Token inválido", "danger")
        return redirect(url_for("login"))
    with get_db() as db:
        db.executescript("DROP TABLE IF EXISTS predictions")
        db.executescript("DROP TABLE IF EXISTS matches")
        db.executescript("DROP TABLE IF EXISTS users")
    init_db()
    logout_user()
    flash("Base de datos reiniciada. Registra el primer usuario como admin.", "success")
    return redirect(url_for("register"))


# Always init DB on import (needed for gunicorn)
init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=True)
