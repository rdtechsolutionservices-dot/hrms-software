from flask import Flask, render_template, request, jsonify, redirect, session, send_file
import threading
import sqlite3, os, io, csv, hashlib
from werkzeug.utils import secure_filename
from datetime import datetime, date, timedelta
import calendar

app = Flask(__name__)
app.secret_key = "VijayshriPackaging2024"
# ── Add Python builtins to Jinja2 globals ────────────────────
# So templates can use enumerate(), zip(), etc. without needing them passed explicitly
app.jinja_env.globals.update(
    enumerate=enumerate, zip=zip, len=len,
    isinstance=isinstance, range=range,
    min=min, max=max, abs=abs, round=round
)
# ── Session timeout: 5 minutes of inactivity ──────────────
from datetime import timedelta
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=10)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

@app.before_request
def refresh_session_timeout():
    """Reset 5-min timeout on every request — logout on inactivity"""
    from flask import request as _req
    if "user" in session:
        session.permanent = True
        session.modified = True

# ── Import Progress Store (in-memory, per session) ──
import_progress = {
    "status":   "idle",   # idle / running / done / error
    "stage":    "",       # "Connecting..." / "Fetching logs..." / "Processing..."
    "total":    0,        # total logs on machine
    "processed":0,        # records processed so far
    "imported": 0,        # records saved to DB
    "skipped":  0,        # unmatched / skipped
    "percent":  0,        # 0-100
    "message":  "",       # final message
    "error":    "",
}

@app.template_filter('datetimeformat')
def datetimeformat(value, fmt='%w'):
    try:
        from datetime import datetime
        return datetime.strptime(str(value), "%Y-%m-%d").strftime(fmt)
    except:
        return "0"

@app.template_filter('hrsmin')
def hrsmin_filter(minutes):
    """Convert minutes to HH:MM format"""
    if not minutes: return "—"
    try:
        m = int(float(minutes))
        if m <= 0: return "—"
        return f"{m//60:02d}:{m%60:02d}"
    except: return "—"

@app.template_filter('otfmt')
def otfmt_filter(minutes):
    """OT minutes to HH:MM format"""
    if not minutes or int(float(minutes)) <= 0: return "—"
    m = int(float(minutes))
    return f"{m//60:02d}:{m%60:02d}"
DB = "vpl_payroll.db"

COMPANY = "Vijayshri Packaging Ltd."
MONTHS  = ["January","February","March","April","May","June",
           "July","August","September","October","November","December"]

# Salary defaults (overridable from DB settings)
HRA_PCT     = 0.40
SPECIAL_PCT = 0.20
PF_PCT      = 0.12
ESI_PCT     = 0.0075
ESI_LIMIT   = 21000
STAFF_IN    = 9*60+30
STAFF_OUT   = 19*60+30
ASSOC_WORK  = 8*60+30
LATE_GRACE  = 15
SHORT_ALLOW = 5*60

# ─────────────────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    # journal_mode=WAL is a DB-level setting, set once at init_db — skip here for speed
    conn.execute("PRAGMA synchronous=NORMAL")   # Balanced safety vs speed
    conn.execute("PRAGMA cache_size=-64000")    # 64MB page cache
    conn.execute("PRAGMA temp_store=MEMORY")    # Temp tables in RAM
    conn.execute("PRAGMA busy_timeout=10000")   # Wait 10s before locked error
    return conn

def hp(p): return hashlib.sha256(p.encode()).hexdigest()

def init_db():
    conn = get_db(); c = conn.cursor()
    # Set WAL mode once at startup — persists for DB file lifetime
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA mmap_size=268435456")   # 256MB memory-mapped I/O
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE, password TEXT, role TEXT DEFAULT 'employee',
        emp_id TEXT, name TEXT, is_active INTEGER DEFAULT 1,
        permissions TEXT DEFAULT '')""")
    try: c.execute("ALTER TABLE users ADD COLUMN permissions TEXT DEFAULT ''")
    except: pass

    # User permissions table (granular)
    c.execute("""CREATE TABLE IF NOT EXISTS user_permissions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        permission TEXT NOT NULL,
        UNIQUE(user_id, permission))""")
    c.execute("""CREATE TABLE IF NOT EXISTS employees (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_code TEXT UNIQUE NOT NULL,
        emp_name TEXT NOT NULL,
        category TEXT DEFAULT 'Associate',
        department TEXT,
        location TEXT,
        designation TEXT,
        date_of_joining TEXT,
        date_of_birth TEXT,
        gender TEXT,
        phone TEXT,
        email TEXT,
        personal_email TEXT,
        official_email TEXT,
        address TEXT,
        aadhar TEXT,
        pan TEXT,
        bank_account TEXT,
        bank_name TEXT,
        ifsc TEXT,
        basic REAL DEFAULT 0,
        hra REAL DEFAULT 0,
        special_allowance REAL DEFAULT 0,
        pf_applicable INTEGER DEFAULT 1,
        esi_applicable INTEGER DEFAULT 1,
        tds_percent REAL DEFAULT 0,
        scheme_id INTEGER DEFAULT NULL,
        uan_number TEXT,
        pf_number TEXT,
        esic_number TEXT,
        status TEXT DEFAULT 'Active',
        resignation_date TEXT,
        last_working_day TEXT,
        exit_reason TEXT)""")
    try: c.execute("ALTER TABLE employees ADD COLUMN scheme_id INTEGER DEFAULT NULL")
    except: pass
    c.execute("""CREATE TABLE IF NOT EXISTS attendance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_code TEXT NOT NULL,
        att_date TEXT NOT NULL,
        in_time TEXT,
        out_time TEXT,
        working_minutes INTEGER DEFAULT 0,
        status TEXT DEFAULT 'Present',
        late_minutes INTEGER DEFAULT 0,
        short_minutes INTEGER DEFAULT 0,
        ot_minutes INTEGER DEFAULT 0,
        is_half_day INTEGER DEFAULT 0,
        UNIQUE(emp_code, att_date))""")
    try: c.execute("ALTER TABLE attendance ADD COLUMN remarks TEXT DEFAULT NULL")
    except: pass
    c.execute("""CREATE TABLE IF NOT EXISTS salary_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_code TEXT NOT NULL, month INTEGER, year INTEGER,
        category TEXT,
        working_days INTEGER DEFAULT 0,
        payable_days REAL DEFAULT 0,
        present_days REAL DEFAULT 0,
        absent_days REAL DEFAULT 0,
        paid_leave_days REAL DEFAULT 0,
        half_days REAL DEFAULT 0,
        wop_days REAL DEFAULT 0,
        holiday_days REAL DEFAULT 0,
        late_marks INTEGER DEFAULT 0,
        per_day_salary REAL DEFAULT 0,
        basic_earned REAL DEFAULT 0,
        hra_earned REAL DEFAULT 0,
        special_earned REAL DEFAULT 0,
        ot_hours REAL DEFAULT 0,
        ot_amount REAL DEFAULT 0,
        bonus REAL DEFAULT 0,
        gross REAL DEFAULT 0,
        pf REAL DEFAULT 0,
        employer_pf REAL DEFAULT 0,
        esi REAL DEFAULT 0,
        employer_esi REAL DEFAULT 0,
        pt REAL DEFAULT 0,
        lwf REAL DEFAULT 0,
        tds REAL DEFAULT 0,
        advance_deduction REAL DEFAULT 0,
        total_deductions REAL DEFAULT 0,
        net_salary REAL DEFAULT 0,
        payment_status TEXT DEFAULT 'Pending',
        payment_date TEXT,
        payment_mode TEXT,
        payment_ref TEXT,
        remarks TEXT,
        locked INTEGER DEFAULT 0,
        generated_on TEXT,
        UNIQUE(emp_code, month, year))""")
    # Add any missing columns to existing DB
    _sr_cols = [r[1] for r in c.execute("PRAGMA table_info(salary_records)").fetchall()]
    for _col in ["payable_days REAL DEFAULT 0","absent_days REAL DEFAULT 0",
                 "paid_leave_days REAL DEFAULT 0","half_days REAL DEFAULT 0",
                 "wop_days REAL DEFAULT 0","holiday_days REAL DEFAULT 0",
                 "late_marks INTEGER DEFAULT 0","per_day_salary REAL DEFAULT 0",
                 "bonus REAL DEFAULT 0","employer_pf REAL DEFAULT 0",
                 "employer_esi REAL DEFAULT 0","pt REAL DEFAULT 0",
                 "lwf REAL DEFAULT 0","advance_deduction REAL DEFAULT 0",
                 "loan_deduction REAL DEFAULT 0","canteen_deduction REAL DEFAULT 0",
                 "fine_deduction REAL DEFAULT 0",
                 "actual_gross REAL DEFAULT 0",
                 "skip_deductions INTEGER DEFAULT 0",
                 "skip_reason TEXT",
                 "payment_status TEXT DEFAULT 'Pending'","payment_date TEXT",
                 "payment_mode TEXT","payment_ref TEXT","remarks TEXT",
                 "locked INTEGER DEFAULT 0"]:
        _cn = _col.split()[0]
        if _cn not in _sr_cols:
            try: c.execute(f"ALTER TABLE salary_records ADD COLUMN {_col}")
            except: pass
    # ── Employee Scheme Master ──────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS employee_schemes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scheme_name TEXT UNIQUE NOT NULL,
        description TEXT,
        pf_applicable INTEGER DEFAULT 1,
        esi_applicable INTEGER DEFAULT 1,
        pt_applicable INTEGER DEFAULT 0,
        bonus_applicable INTEGER DEFAULT 1,
        gratuity_applicable INTEGER DEFAULT 1,
        lwf_applicable INTEGER DEFAULT 0,
        tds_applicable INTEGER DEFAULT 1,
        ot_applicable INTEGER DEFAULT 1,
        notes TEXT,
        is_active INTEGER DEFAULT 1,
        created_on TEXT)""")
    # Default schemes
    try:
        c.execute("SELECT COUNT(*) FROM employee_schemes")
        if c.fetchone()[0] == 0:
            for nm,desc,pf,esi,pt,bon,grat,lwf,tds,ot in [
                ("Regular","Standard employee - all deductions",1,1,0,1,1,0,1,1),
                ("NAPS","National Apprenticeship - No PF/Bonus",0,1,0,0,0,0,0,1),
                ("Contract","Contract employee",1,1,0,0,0,0,0,1),
                ("Trainee","Trainee - No PF/Gratuity",0,0,0,0,0,0,0,1),
            ]:
                conn.execute("""INSERT OR IGNORE INTO employee_schemes
                    (scheme_name,description,pf_applicable,esi_applicable,pt_applicable,
                     bonus_applicable,gratuity_applicable,lwf_applicable,tds_applicable,ot_applicable,is_active,created_on)
                    VALUES (?,?,?,?,?,?,?,?,?,?,1,date('now'))""",
                    (nm,desc,pf,esi,pt,bon,grat,lwf,tds,ot))
    except: pass

    # ── OT Rate Master ───────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS ot_rate_master (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        rate_type TEXT DEFAULT 'multiplier',
        multiplier REAL DEFAULT 1.0,
        fixed_rate REAL DEFAULT 0,
        description TEXT,
        is_active INTEGER DEFAULT 1,
        updated_on TEXT)""")
    try:
        c.execute("SELECT COUNT(*) FROM ot_rate_master")
        if c.fetchone()[0] == 0:
            for cat,rtype,mult,fixed,desc in [
                ("Associate","gross_ot",1.3,0,"(Actual Gross÷208)×1.3 — OT Rate"),
            ]:
                conn.execute("""INSERT OR IGNORE INTO ot_rate_master
                    (category,rate_type,multiplier,fixed_rate,description,is_active,updated_on)
                    VALUES (?,?,?,?,?,1,date('now'))""",
                    (cat,rtype,mult,fixed,desc))
    except: pass

    c.execute("""CREATE TABLE IF NOT EXISTS leaves (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_code TEXT, leave_type TEXT, from_date TEXT,
        to_date TEXT, days REAL, reason TEXT,
        status TEXT DEFAULT 'Pending', applied_on TEXT)""")

    # ── Salary Components Master ─────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS salary_components (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        component_name TEXT UNIQUE NOT NULL,
        component_type TEXT DEFAULT 'earning',
        calc_type TEXT DEFAULT 'fixed',
        value REAL DEFAULT 0,
        is_taxable INTEGER DEFAULT 1,
        is_active INTEGER DEFAULT 1,
        created_on TEXT)""")
    try:
        c.execute("SELECT COUNT(*) FROM salary_components")
        if c.fetchone()[0] == 0:
            for nm,ct,calc,val,tax in [
                ("Basic","earning","percentage",40.0,1),
                ("HRA","earning","percentage",40.0,0),
                ("Special Allowance","earning","percentage",20.0,1),
                ("Conveyance","earning","fixed",1600,0),
                ("Medical","earning","fixed",1250,0),
                ("PF Employee","deduction","percentage",12.0,0),
                ("PF Employer","employer","percentage",12.0,0),
                ("ESIC Employee","deduction","percentage",0.75,0),
                ("ESIC Employer","employer","percentage",3.25,0),
                ("Professional Tax","deduction","slab",0,0),
                ("TDS","deduction","formula",0,0),
                ("LWF","deduction","fixed",0,0),
            ]:
                conn.execute("""INSERT OR IGNORE INTO salary_components
                    (component_name,component_type,calc_type,value,is_taxable,is_active,created_on)
                    VALUES (?,?,?,?,?,1,date('now'))""", (nm,ct,calc,val,tax))
    except: pass

    # ── PT Slab Master (Madhya Pradesh default) ──────────────
    c.execute("""CREATE TABLE IF NOT EXISTS pt_slabs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        state TEXT DEFAULT 'Madhya Pradesh',
        salary_from REAL DEFAULT 0,
        salary_to REAL DEFAULT 0,
        pt_amount REAL DEFAULT 0,
        frequency TEXT DEFAULT 'monthly',
        is_active INTEGER DEFAULT 1)""")
    try:
        c.execute("SELECT COUNT(*) FROM pt_slabs")
        if c.fetchone()[0] == 0:
            # MP PT slabs
            for sf,st,amt in [(0,18999,0),(19000,25000,208),(25001,99999999,212)]:
                conn.execute("""INSERT OR IGNORE INTO pt_slabs
                    (state,salary_from,salary_to,pt_amount,is_active)
                    VALUES ('Madhya Pradesh',?,?,?,1)""", (sf,st,amt))
    except: pass

    # ── Payroll Settings ─────────────────────────────────────
    # ── Monthly Working Days (per month override) ───────────────
    c.execute("""CREATE TABLE IF NOT EXISTS monthly_working_days (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        year INTEGER NOT NULL,
        month INTEGER NOT NULL,
        staff_days INTEGER DEFAULT 26,
        nonstaff_days INTEGER DEFAULT 26,
        notes TEXT,
        created_by TEXT,
        created_on TEXT,
        UNIQUE(year, month))""")

    c.execute("""CREATE TABLE IF NOT EXISTS payroll_settings (
        id INTEGER PRIMARY KEY DEFAULT 1,
        company_name TEXT DEFAULT 'Vijayshri Packaging Ltd.',
        pf_employer_pct REAL DEFAULT 12.0,
        pf_employee_pct REAL DEFAULT 12.0,
        esic_employer_pct REAL DEFAULT 3.25,
        esic_employee_pct REAL DEFAULT 0.75,
        esic_wage_limit REAL DEFAULT 21000,
        pf_wage_limit REAL DEFAULT 15000,
        pt_state TEXT DEFAULT 'Madhya Pradesh',
        pt_applicable INTEGER DEFAULT 0,
        lwf_amount REAL DEFAULT 0,
        working_days_month INTEGER DEFAULT 26,
        ot_rate_formula TEXT DEFAULT 'basic_div_26_div_shift',
        late_grace_minutes INTEGER DEFAULT 15,
        late_halfday_count INTEGER DEFAULT 3,
        el_per_year REAL DEFAULT 16,
        cl_per_year REAL DEFAULT 6,
        payment_day INTEGER DEFAULT 7,
        min_basic REAL DEFAULT 0,
        updated_on TEXT)""")
    conn.execute("INSERT OR IGNORE INTO payroll_settings (id) VALUES (1)")

    # ── Enhanced Salary Records — safe column migration ──────
    salary_extra_cols = [
        "pt REAL DEFAULT 0",
        "lwf REAL DEFAULT 0",
        "bonus REAL DEFAULT 0",
        "arrears REAL DEFAULT 0",
        "advance_deduction REAL DEFAULT 0",
        "employer_pf REAL DEFAULT 0",
        "employer_esi REAL DEFAULT 0",
        "absent_days REAL DEFAULT 0",
        "paid_leave_days REAL DEFAULT 0",
        "half_days REAL DEFAULT 0",
        "late_marks INTEGER DEFAULT 0",
        "wop_days REAL DEFAULT 0",
        "holiday_days REAL DEFAULT 0",
        "payable_days REAL DEFAULT 0",
        "per_day_salary REAL DEFAULT 0",
        "payment_status TEXT DEFAULT 'Pending'",
        "payment_date TEXT",
        "payment_mode TEXT",
        "payment_ref TEXT",
        "remarks TEXT",
        "locked INTEGER DEFAULT 0",
        "payslip_code TEXT DEFAULT NULL",
    ]
    # Get existing columns
    existing_cols = {row[1] for row in c.execute("PRAGMA table_info(salary_records)").fetchall()}
    for col_def in salary_extra_cols:
        col_name = col_def.split()[0]
        if col_name not in existing_cols:
            try: c.execute(f"ALTER TABLE salary_records ADD COLUMN {col_def}")
            except: pass

    # ── Payroll Audit Log ────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS payroll_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_code TEXT,
        month INTEGER, year INTEGER,
        action TEXT,
        old_value TEXT,
        new_value TEXT,
        changed_by TEXT,
        changed_on TEXT)""")

    # ── Bonus Master ─────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS payroll_bonus (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_code TEXT NOT NULL,
        bonus_type TEXT DEFAULT 'festival',
        amount REAL DEFAULT 0,
        month INTEGER,
        year INTEGER,
        remarks TEXT,
        created_by TEXT,
        created_on TEXT)""")

    # ── Payment Tracking ─────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS payroll_payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_code TEXT NOT NULL,
        month INTEGER, year INTEGER,
        net_amount REAL DEFAULT 0,
        payment_mode TEXT DEFAULT 'Bank Transfer',
        payment_date TEXT,
        payment_ref TEXT,
        bank_account TEXT,
        status TEXT DEFAULT 'Pending',
        processed_by TEXT,
        processed_on TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS increments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_code TEXT, effective_date TEXT,
        old_basic REAL, new_basic REAL, reason TEXT, done_on TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS holidays (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        holiday_date TEXT NOT NULL,
        title TEXT NOT NULL,
        holiday_type TEXT DEFAULT 'National',
        description TEXT,
        is_optional INTEGER DEFAULT 0,
        added_by TEXT,
        created_on TEXT,
        UNIQUE(holiday_date, title))""")
    c.execute("""CREATE TABLE IF NOT EXISTS email_settings (
        id INTEGER PRIMARY KEY DEFAULT 1,
        provider TEXT DEFAULT 'gmail',
        smtp_host TEXT DEFAULT 'smtp.gmail.com',
        smtp_port INTEGER DEFAULT 587,
        email TEXT DEFAULT '',
        password TEXT DEFAULT '',
        sender_name TEXT DEFAULT 'Vijayshri Packaging Ltd.',
        is_active INTEGER DEFAULT 0)""")
    c.execute("""CREATE TABLE IF NOT EXISTS email_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        to_email TEXT, subject TEXT, type TEXT,
        status TEXT, sent_on TEXT, error TEXT)""")
    # Custom Deductions Table
    c.execute("""CREATE TABLE IF NOT EXISTS custom_deductions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_code TEXT NOT NULL,
        deduction_type TEXT NOT NULL,
        description TEXT,
        total_amount REAL DEFAULT 0,
        monthly_amount REAL DEFAULT 0,
        amount_deducted REAL DEFAULT 0,
        start_month INTEGER,
        start_year INTEGER,
        end_month INTEGER DEFAULT NULL,
        end_year INTEGER DEFAULT NULL,
        deduction_mode TEXT DEFAULT 'loan',
        status TEXT DEFAULT 'Active',
        created_on TEXT,
        created_by TEXT)""")
    try: c.execute("ALTER TABLE custom_deductions ADD COLUMN end_month INTEGER DEFAULT NULL")
    except: pass
    try: c.execute("ALTER TABLE custom_deductions ADD COLUMN end_year INTEGER DEFAULT NULL")
    except: pass
    try: c.execute("ALTER TABLE custom_deductions ADD COLUMN deduction_mode TEXT DEFAULT 'loan'")
    except: pass

    # Punch Alerts Table
    c.execute("""CREATE TABLE IF NOT EXISTS punch_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_code TEXT NOT NULL,
        emp_name TEXT,
        alert_date TEXT NOT NULL,
        alert_type TEXT NOT NULL,
        details TEXT,
        status TEXT DEFAULT 'Pending',
        resolved_by TEXT,
        resolved_on TEXT,
        UNIQUE(emp_code, alert_date, alert_type))""")

    # Insert default email settings row
    c.execute("INSERT OR IGNORE INTO email_settings (id) VALUES (1)")
    
    # No default holidays pre-loaded. Admin will add holidays manually.
    c.execute("""CREATE TABLE IF NOT EXISTS leave_balance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_code TEXT NOT NULL,
        year INTEGER NOT NULL,
        cl_allotted REAL DEFAULT 0,
        sl_allotted REAL DEFAULT 0,
        el_allotted REAL DEFAULT 0,
        cl_used REAL DEFAULT 0,
        sl_used REAL DEFAULT 0,
        el_used REAL DEFAULT 0,
        el_carried REAL DEFAULT 0,
        cl_pending REAL DEFAULT 0,
        el_pending REAL DEFAULT 0,
        UNIQUE(emp_code, year))""")
    try: c.execute("ALTER TABLE payroll_settings ADD COLUMN min_basic REAL DEFAULT 0")
    except: pass
    try: c.execute("ALTER TABLE leave_balance ADD COLUMN cl_pending REAL DEFAULT 0")
    except: pass
    try: c.execute("ALTER TABLE leave_balance ADD COLUMN el_pending REAL DEFAULT 0")
    except: pass


    # ── Leave Master Settings ─────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS leave_master_settings (
        id INTEGER PRIMARY KEY,
        period_type TEXT DEFAULT 'calendar',  -- 'calendar' or 'financial'
        financial_year_start_month INTEGER DEFAULT 4,  -- April=4
        financial_year_start_day INTEGER DEFAULT 1,
        earn_auto_credit INTEGER DEFAULT 1,  -- auto credit earned leaves
        updated_by TEXT, updated_on TEXT)""")
    c.execute("INSERT OR IGNORE INTO leave_master_settings (id,period_type) VALUES (1,'financial')")

    # Enhance leave_types with earn rules
    try: c.execute("ALTER TABLE leave_types ADD COLUMN earn_enabled INTEGER DEFAULT 0")
    except: pass
    try: c.execute("ALTER TABLE leave_types ADD COLUMN earn_every_n_days REAL DEFAULT 30")
    except: pass
    try: c.execute("ALTER TABLE leave_types ADD COLUMN earn_days_per_period REAL DEFAULT 0.75")
    except: pass
    try: c.execute("ALTER TABLE leave_types ADD COLUMN max_accumulation REAL DEFAULT 0")
    except: pass
    try: c.execute("ALTER TABLE leave_types ADD COLUMN applicable_to_nonstaff INTEGER DEFAULT 0")
    except: pass

    # Leave balance: add period tracking
    try: c.execute("ALTER TABLE leave_balance ADD COLUMN period_label TEXT DEFAULT ''")
    except: pass


    # ── Leave Types Master ───────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS leave_types (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        code TEXT UNIQUE NOT NULL,
        name TEXT NOT NULL,
        is_paid INTEGER DEFAULT 1,
        annual_quota REAL DEFAULT 0,
        applicable_to TEXT DEFAULT 'Staff',
        carry_forward INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1)""")
    try:
        c.execute("SELECT COUNT(*) FROM leave_types")
        if c.fetchone()[0] == 0:
            for code,name,paid,quota,appl,carry in [
                ("EL","Earned Leave",1,16,"Staff",1),
                ("CL","Casual Leave",1,6,"Staff",0),
                ("SL","Sick Leave",1,6,"Staff",0),
                ("LWP","Leave Without Pay",0,0,"All",0),
                ("CO","Comp Off",1,0,"All",0),
                ("ML","Maternity Leave",1,182,"Staff",0),
            ]:
                conn.execute("""INSERT OR IGNORE INTO leave_types
                    (code,name,is_paid,annual_quota,applicable_to,carry_forward,is_active)
                    VALUES (?,?,?,?,?,?,1)""", (code,name,paid,quota,appl,carry))
    except: pass
    c.execute("""CREATE TABLE IF NOT EXISTS leave_requests (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_code TEXT NOT NULL,
        emp_name TEXT,
        leave_type TEXT NOT NULL,
        from_date TEXT NOT NULL,
        to_date TEXT NOT NULL,
        days REAL DEFAULT 1,
        is_half_day INTEGER DEFAULT 0,
        reason TEXT,
        status TEXT DEFAULT 'Pending',
        applied_on TEXT,
        approved_by TEXT,
        approved_on TEXT,
        rejection_reason TEXT,
        source TEXT DEFAULT 'employee',
        remarks TEXT)""")
    try: c.execute("ALTER TABLE leave_requests ADD COLUMN source TEXT DEFAULT 'employee'")
    except: pass
    try: c.execute("ALTER TABLE leave_requests ADD COLUMN remarks TEXT")
    except: pass

    # ── Associate Leave Records (OT Process) ───────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS associate_leave_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_code TEXT NOT NULL,
        emp_name TEXT,
        month INTEGER NOT NULL,
        year INTEGER NOT NULL,
        absent_date TEXT NOT NULL,
        leave_status TEXT DEFAULT 'Pending',
        approved_by TEXT,
        approved_on TEXT,
        remarks TEXT,
        UNIQUE(emp_code, absent_date))""")
    try: c.execute("ALTER TABLE associate_leave_records ADD COLUMN remarks TEXT")
    except: pass


    # Letter settings (header/footer images + auto increment counters)
    c.execute("""CREATE TABLE IF NOT EXISTS letter_settings (
        id INTEGER PRIMARY KEY,
        header_image BLOB, header_filename TEXT,
        footer_image BLOB, footer_filename TEXT,
        seal_image BLOB, seal_filename TEXT,
        updated_on TEXT)""")
    try: c.execute("ALTER TABLE letter_settings ADD COLUMN seal_image BLOB")
    except: pass
    try: c.execute("ALTER TABLE letter_settings ADD COLUMN seal_filename TEXT")
    except: pass
    try: c.execute("ALTER TABLE letter_settings ADD COLUMN signature_image BLOB")
    except: pass
    try: c.execute("ALTER TABLE letter_settings ADD COLUMN signature_filename TEXT")
    except: pass
    try: c.execute("ALTER TABLE letter_settings ADD COLUMN director_sign_image BLOB")
    except: pass
    try: c.execute("ALTER TABLE letter_settings ADD COLUMN director_sign_filename TEXT")
    except: pass
    try: c.execute("ALTER TABLE letter_settings ADD COLUMN hr_sign_image BLOB")
    except: pass
    try: c.execute("ALTER TABLE letter_settings ADD COLUMN hr_sign_filename TEXT")
    except: pass
    c.execute("""CREATE TABLE IF NOT EXISTS letter_counters (
        letter_type TEXT PRIMARY KEY,
        last_number INTEGER DEFAULT 0,
        prefix TEXT DEFAULT '')""")
    # Seed letter types
    for lt, pfx in [("experience","EXP"),("relieving","REL"),("increment","INC"),("appointment","APT"),("warning","WRN"),("other","LTR")]:
        c.execute("INSERT OR IGNORE INTO letter_counters (letter_type,last_number,prefix) VALUES (?,0,?)", (lt,pfx))


    # Document log — every generated document tracked
    c.execute("""CREATE TABLE IF NOT EXISTS document_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        doc_code TEXT UNIQUE NOT NULL,
        doc_type TEXT NOT NULL,
        emp_code TEXT,
        emp_name TEXT,
        generated_on TEXT,
        generated_by TEXT,
        details TEXT)""")
    # Improve letter_counters with year-wise reset option
    try: c.execute("ALTER TABLE letter_counters ADD COLUMN year_prefix INTEGER DEFAULT 0")
    except: pass


    # Department OT Limits (analytics only - not enforcement)
    c.execute("""CREATE TABLE IF NOT EXISTS dept_ot_limits (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        department TEXT UNIQUE NOT NULL,
        monthly_ot_limit_hrs REAL DEFAULT 50,
        per_day_hrs_per_emp REAL DEFAULT 2.5,
        alert_threshold_pct REAL DEFAULT 80,
        updated_by TEXT, updated_on TEXT)""")
    try: c.execute("ALTER TABLE dept_ot_limits ADD COLUMN per_day_hrs_per_emp REAL DEFAULT 2.5")
    except: pass


    # WhatsApp notification settings
    c.execute("""CREATE TABLE IF NOT EXISTS whatsapp_settings (
        id INTEGER PRIMARY KEY,
        is_active INTEGER DEFAULT 0,
        method TEXT DEFAULT 'callmebot',
        api_key TEXT DEFAULT '',
        phone TEXT DEFAULT '',
        send_birthday INTEGER DEFAULT 1,
        send_anniversary INTEGER DEFAULT 1,
        updated_on TEXT)""")
    c.execute("INSERT OR IGNORE INTO whatsapp_settings (id) VALUES (1)")
    # Company settings - logo etc
    c.execute("""CREATE TABLE IF NOT EXISTS company_settings (
        id INTEGER PRIMARY KEY,
        company_name TEXT DEFAULT 'Vijayshri Packaging Ltd.',
        logo BLOB,
        logo_filename TEXT,
        updated_on TEXT)""")
    c.execute("INSERT OR IGNORE INTO company_settings (id) VALUES (1)")



    # User activity log
    c.execute("""CREATE TABLE IF NOT EXISTS activity_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER,
        username TEXT,
        action TEXT NOT NULL,
        page TEXT,
        details TEXT,
        ip_address TEXT,
        logged_at TEXT DEFAULT (datetime('now')))""")

    c.execute("SELECT COUNT(*) FROM users")
    if c.fetchone()[0] == 0:
        for u,p,r in [("admin","Admin@123","admin"),("manager","Manager@123","manager")]:
            conn.execute("INSERT INTO users (username,password,role,name) VALUES (?,?,?,?)",
                         (u,hp(p),r,u.capitalize()))


    # ── Machines ──
    c.execute("""CREATE TABLE IF NOT EXISTS machines (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        machine_name TEXT NOT NULL, ip_address TEXT NOT NULL,
        port INTEGER DEFAULT 4370, password INTEGER DEFAULT 0,
        location TEXT, is_active INTEGER DEFAULT 1,
        last_sync TEXT, last_sync_count INTEGER DEFAULT 0,
        serial_number TEXT,
        connection_mode TEXT DEFAULT 'zk',
        adms_last_seen TEXT,
        created_on TEXT)""")
    # Migrate existing machines table
    _m_cols = [r[1] for r in c.execute("PRAGMA table_info(machines)").fetchall()]
    for _mc, _md in [("serial_number", "TEXT"), ("connection_mode", "TEXT DEFAULT 'zk'"),
                     ("adms_last_seen", "TEXT")]:
        if _mc not in _m_cols:
            try: c.execute(f"ALTER TABLE machines ADD COLUMN {_mc} {_md}")
            except: pass

    # ── Shifts ──
    c.execute("""CREATE TABLE IF NOT EXISTS shifts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        shift_name TEXT UNIQUE NOT NULL,
        shift_code TEXT UNIQUE NOT NULL,
        category TEXT DEFAULT 'Associate',
        start_time TEXT NOT NULL,
        end_time TEXT NOT NULL,
        is_next_day INTEGER DEFAULT 0,
        allowed_in_start TEXT DEFAULT NULL,
        allowed_in_end TEXT DEFAULT NULL,
        punch_begin_before INTEGER DEFAULT 60,
        punch_end_after INTEGER DEFAULT 120,
        working_hours REAL DEFAULT 8.5,
        grace_minutes INTEGER DEFAULT 15,
        ot_formula TEXT DEFAULT 'total_minus_shift',
        half_day_minutes INTEGER DEFAULT 240,
        neglect_last_in INTEGER DEFAULT 0,
        is_night_shift INTEGER DEFAULT 0,
        is_active INTEGER DEFAULT 1,
        created_on TEXT)""")
    # Migrate existing DB — add new columns if missing
    for col, defval in [
        ("is_next_day",         "INTEGER DEFAULT 0"),
        ("punch_begin_before",  "INTEGER DEFAULT 60"),
        ("punch_end_after",     "INTEGER DEFAULT 120"),
        ("ot_formula",          "TEXT DEFAULT 'total_minus_shift'"),
        ("half_day_minutes",    "INTEGER DEFAULT 240"),
        ("neglect_last_in",     "INTEGER DEFAULT 0"),
        ("allowed_in_start",    "TEXT DEFAULT NULL"),
        ("allowed_in_end",      "TEXT DEFAULT NULL"),
        ("half_day_min_minutes", "INTEGER DEFAULT 180"),
        ("full_day_min_minutes", "INTEGER DEFAULT 390"),
    ]:
        try: c.execute(f"ALTER TABLE shifts ADD COLUMN {col} {defval}")
        except: pass

    # ── Employee Shifts ──
    c.execute("""CREATE TABLE IF NOT EXISTS employee_shifts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_code TEXT UNIQUE NOT NULL,
        shift_id INTEGER,
        shift_mode TEXT DEFAULT 'auto',
        effective_from TEXT,
        assigned_by TEXT,
        assigned_on TEXT)""")
    try: c.execute("ALTER TABLE employee_shifts ADD COLUMN shift_mode TEXT DEFAULT 'auto'")
    except: pass

    # ── Masters Tables ──
    c.execute("""CREATE TABLE IF NOT EXISTS master_categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL, is_active INTEGER DEFAULT 1)""")
    c.execute("""CREATE TABLE IF NOT EXISTS master_departments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL, is_active INTEGER DEFAULT 1)""")
    c.execute("""CREATE TABLE IF NOT EXISTS master_designations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL, is_active INTEGER DEFAULT 1)""")
    c.execute("""CREATE TABLE IF NOT EXISTS master_locations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL, is_active INTEGER DEFAULT 1)""")

    # Pre-load defaults
    c.execute("SELECT COUNT(*) FROM master_categories")
    if c.fetchone()[0] == 0:
        for v in ["Staff","Associate"]:
            conn.execute("INSERT OR IGNORE INTO master_categories (name) VALUES (?)",(v,))
    c.execute("SELECT COUNT(*) FROM master_departments")
    if c.fetchone()[0] == 0:
        for v in ["HALL - 1","HALL - 2","HALL - 3","HALL - 4","HALL - 5","HALL - 6","HALL - 7",
                  "STAFF","CANTEEN","SECURITY","QUALITY CONTROL","HOUSEKEEPING","DISPATCH","ENGINEERING STORE"]:
            conn.execute("INSERT OR IGNORE INTO master_departments (name) VALUES (?)",(v,))
    c.execute("SELECT COUNT(*) FROM master_designations")
    if c.fetchone()[0] == 0:
        for v in ["OPERATOR","HELPER","SUPERVISOR","EXECUTIVE","MANAGER","SECURITY GUARD",
                  "CANTEEN WORKER","TECHNICIAN","STORE KEEPER","QUALITY INSPECTOR"]:
            conn.execute("INSERT OR IGNORE INTO master_designations (name) VALUES (?)",(v,))
    c.execute("SELECT COUNT(*) FROM master_locations")
    if c.fetchone()[0] == 0:
        for v in ["HALL - 1","HALL - 2","HALL - 3","HALL - 4","HALL - 5","HALL - 6","HALL - 7",
                  "STAFF","CANTEEN","SECURITY","QUALITY CONTROL","HOUSEKEEPING","DISPATCH","ENGINEERING STORE"]:
            conn.execute("INSERT OR IGNORE INTO master_locations (name) VALUES (?)",(v,))

    # Add columns to holidays if not exists
    try: c.execute("ALTER TABLE holidays ADD COLUMN applies_to TEXT DEFAULT 'All'")
    except: pass
    try: c.execute("ALTER TABLE holidays ADD COLUMN is_paid INTEGER DEFAULT 1")
    except: pass

    # ── Shift Groups ──
    c.execute("""CREATE TABLE IF NOT EXISTS shift_groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_name TEXT UNIQUE NOT NULL,
        description TEXT,
        is_active INTEGER DEFAULT 1,
        created_on TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS shift_group_members (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        group_id INTEGER NOT NULL,
        shift_id INTEGER NOT NULL,
        UNIQUE(group_id, shift_id))""")
    c.execute("""CREATE TABLE IF NOT EXISTS employee_shift_groups (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_code TEXT UNIQUE NOT NULL,
        group_id INTEGER NOT NULL,
        assigned_by TEXT,
        assigned_on TEXT)""")
    # Existing DB: add shift_name to attendance
    try: c.execute("ALTER TABLE attendance ADD COLUMN shift_name TEXT DEFAULT ''")
    except: pass
    # Performance indexes for faster salary calculation
    try: c.execute("CREATE INDEX IF NOT EXISTS idx_att_emp_date ON attendance(emp_code, att_date)")
    except: pass
    try: c.execute("CREATE INDEX IF NOT EXISTS idx_att_month_year ON attendance(emp_code)")
    except: pass
    try: c.execute("CREATE INDEX IF NOT EXISTS idx_sal_emp_month ON salary_records(emp_code, month, year)")
    except: pass
    # Pre-load General Shift Group
    c.execute("SELECT COUNT(*) FROM shift_groups")
    if c.fetchone()[0] == 0:
        conn.execute("INSERT INTO shift_groups (group_name,description,is_active,created_on) VALUES (?,?,1,date('now'))",
                    ("General Shift","Day (08:00-16:30) & Night (18:30-03:00) rotational"))
        conn.execute("INSERT OR IGNORE INTO shift_group_members (group_id,shift_id) SELECT 1,id FROM shifts WHERE shift_code IN ('GS','NS')")

    # ── Masters Tables ──
    c.execute("""CREATE TABLE IF NOT EXISTS master_categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL, is_active INTEGER DEFAULT 1)""")
    c.execute("""CREATE TABLE IF NOT EXISTS master_departments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL, is_active INTEGER DEFAULT 1)""")
    c.execute("""CREATE TABLE IF NOT EXISTS master_designations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL, is_active INTEGER DEFAULT 1)""")
    # Pre-load defaults
    for _cat in ["Staff","Associate"]:
        c.execute("INSERT OR IGNORE INTO master_categories (name) VALUES (?)",(_cat,))
    for _dept in ["HALL - 1","HALL - 2","HALL - 3","HALL - 4","HALL - 5","HALL - 6","HALL - 7",
                  "STAFF","CANTEEN","SECURITY","QUALITY CONTROL","HOUSEKEEPING","DISPATCH",
                  "ENGINEERING STORE","PRINTING"]:
        c.execute("INSERT OR IGNORE INTO master_departments (name) VALUES (?)",(_dept,))
    for _desig in ["OPERATOR","HELPER","SUPERVISOR","EXECUTIVE","MANAGER","OFFICER",
                   "TECHNICIAN","GUARD","INCHARGE","SECURITY GUARD"]:
        c.execute("INSERT OR IGNORE INTO master_designations (name) VALUES (?)",(_desig,))
    # Add paid holiday columns
    try: c.execute("ALTER TABLE holidays ADD COLUMN is_paid INTEGER DEFAULT 1")
    except: pass
    try: c.execute("ALTER TABLE holidays ADD COLUMN applies_to TEXT DEFAULT 'All'")
    except: pass

    # ── Punch Alerts ──
    c.execute("""CREATE TABLE IF NOT EXISTS punch_alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_code TEXT NOT NULL, emp_name TEXT,
        alert_date TEXT NOT NULL, alert_type TEXT NOT NULL,
        shift_name TEXT, details TEXT,
        status TEXT DEFAULT 'Pending',
        resolved_by TEXT, resolved_on TEXT,
        UNIQUE(emp_code, alert_date, alert_type))""")

    # ── Salary Settings ──
    c.execute("""CREATE TABLE IF NOT EXISTS salary_settings (
        id INTEGER PRIMARY KEY DEFAULT 1,
        hra_pct REAL DEFAULT 40.0,
        special_pct REAL DEFAULT 20.0,
        pf_pct REAL DEFAULT 12.0,
        esi_pct REAL DEFAULT 0.75,
        esi_limit REAL DEFAULT 21000,
        ot_days_divisor REAL DEFAULT 26,
        ot_hours_divisor REAL DEFAULT 8,
        late_grace_minutes INTEGER DEFAULT 15,
        short_time_limit_hrs REAL DEFAULT 5.0,
        updated_by TEXT, updated_on TEXT)""")

    # ── Shift Roster Dates (date-wise shift assignment) ──
    c.execute("""CREATE TABLE IF NOT EXISTS shift_roster_dates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_code TEXT NOT NULL,
        shift_id INTEGER NOT NULL,
        shift_date TEXT NOT NULL,
        assigned_by TEXT,
        assigned_on TEXT,
        UNIQUE(emp_code, shift_date))""")

    # ── Manpower Master (standard headcount per dept) ──
    c.execute("""CREATE TABLE IF NOT EXISTS dept_manpower (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        department TEXT UNIQUE NOT NULL,
        std_staff INTEGER DEFAULT 0,
        std_nonstaff INTEGER DEFAULT 0,
        updated_by TEXT,
        updated_on TEXT)""")

    # dept_manpower already created above

    # Add applies_to and department_wise to holidays
    try: c.execute("ALTER TABLE holidays ADD COLUMN applies_to TEXT DEFAULT 'All'")
    except: pass
    try: c.execute("ALTER TABLE holidays ADD COLUMN is_paid INTEGER DEFAULT 1")
    except: pass
    try: c.execute("ALTER TABLE holidays ADD COLUMN department_list TEXT DEFAULT ''")
    except: pass

    # ── App Settings (auto import time etc.) ──
    c.execute("""CREATE TABLE IF NOT EXISTS app_settings (
        key TEXT PRIMARY KEY, value TEXT)""")
    c.execute("INSERT OR IGNORE INTO app_settings (key,value) VALUES ('auto_import_time','08:00')")
    c.execute("INSERT OR IGNORE INTO app_settings (key,value) VALUES ('adms_server_address','')")
    c.execute("INSERT OR IGNORE INTO app_settings (key,value) VALUES ('adms_server_port','5000')")

    # Default salary settings
    c.execute("SELECT COUNT(*) FROM salary_settings")
    if c.fetchone()[0] == 0:
        conn.execute("INSERT INTO salary_settings (id) VALUES (1)")

    # Add new columns if they don't exist (for existing databases)
    try: c.execute("ALTER TABLE employees ADD COLUMN personal_email TEXT")
    except: pass
    try: c.execute("ALTER TABLE employees ADD COLUMN official_email TEXT")
    except: pass
    try: c.execute("ALTER TABLE employees ADD COLUMN location TEXT")
    except: pass
    try: c.execute("ALTER TABLE employees ADD COLUMN uan_number TEXT")
    except: pass
    try: c.execute("ALTER TABLE employees ADD COLUMN weekly_off TEXT DEFAULT 'Sunday'")
    except: pass
    c.execute("""CREATE TABLE IF NOT EXISTS employee_custom_fields (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        field_name TEXT NOT NULL UNIQUE,
        field_label TEXT NOT NULL,
        field_type TEXT DEFAULT 'text',
        options TEXT DEFAULT NULL,
        is_required INTEGER DEFAULT 0,
        display_order INTEGER DEFAULT 99,
        is_active INTEGER DEFAULT 1,
        in_export INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )""")
    try: c.execute("ALTER TABLE employee_custom_fields ADD COLUMN options TEXT DEFAULT NULL")
    except: pass
    c.execute("""CREATE TABLE IF NOT EXISTS employee_custom_values (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_code TEXT NOT NULL,
        field_name TEXT NOT NULL,
        field_value TEXT,
        UNIQUE(emp_code, field_name)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS ot_payment_locks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        month INTEGER NOT NULL,
        year INTEGER NOT NULL,
        locked_by TEXT NOT NULL,
        locked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(month, year))""")
    c.execute("""CREATE TABLE IF NOT EXISTS payroll_locks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        month INTEGER NOT NULL,
        year INTEGER NOT NULL,
        locked_by TEXT NOT NULL,
        locked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(month, year)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS user_dept_access (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        department TEXT NOT NULL,
        UNIQUE(user_id, department)
    )""")
    # Performance indexes
    for idx_sql in [
        "CREATE INDEX IF NOT EXISTS idx_att_empdate ON attendance(emp_code, att_date)",
        "CREATE INDEX IF NOT EXISTS idx_att_date ON attendance(att_date)",
        "CREATE INDEX IF NOT EXISTS idx_sal_empmy ON salary_records(emp_code, month, year)",
        "CREATE INDEX IF NOT EXISTS idx_sal_my ON salary_records(month, year)",
        "CREATE INDEX IF NOT EXISTS idx_emp_dept ON employees(department, status)",
        "CREATE INDEX IF NOT EXISTS idx_emp_status ON employees(status)",
    ]:
        try: c.execute(idx_sql)
        except: pass
    # Migrate Non-Staff → Associate in all tables
    try:
        conn.execute("UPDATE employees SET category='Associate' WHERE category='Non-Staff'")
        conn.execute("UPDATE attendance SET category='Associate' WHERE category='Non-Staff'")
        conn.execute("UPDATE salary_records SET category='Associate' WHERE category='Non-Staff'")
        conn.execute("UPDATE ot_rate_master SET category='Associate' WHERE category='Non-Staff'")
        conn.execute("UPDATE leave_requests SET category='Associate' WHERE category='Non-Staff'")
        conn.commit()
    except: pass
    try: c.execute("ALTER TABLE employees ADD COLUMN pf_number TEXT")
    except: pass
    try: c.execute("ALTER TABLE employees ADD COLUMN esic_number TEXT")
    except: pass
    # ── punch_alerts column migrations ───────────────────────
    try: c.execute("ALTER TABLE punch_alerts ADD COLUMN shift_name TEXT")
    except: pass
    try: c.execute("ALTER TABLE punch_alerts ADD COLUMN emp_name TEXT")
    except: pass
    # ── Manual entry lock flag ────────────────────────────────
    try: c.execute("ALTER TABLE attendance ADD COLUMN is_manual INTEGER DEFAULT 0")
    except: pass
    # ── Payroll settings new columns ──────────────────────────
    try: c.execute("ALTER TABLE payroll_settings ADD COLUMN short_time_limit_hrs REAL DEFAULT 5.0")
    except: pass
    try: c.execute("ALTER TABLE payroll_settings ADD COLUMN late_free_days INTEGER DEFAULT 2")
    except: pass
    try: c.execute("ALTER TABLE payroll_settings ADD COLUMN short_time_per_halfday REAL DEFAULT 2.5")
    except: pass
    # ── Late waiver flag ─────────────────────────────────────
    try: c.execute("ALTER TABLE attendance ADD COLUMN late_waived INTEGER DEFAULT 0")
    except: pass
    # ── Raw Punch Log (eSSL-style individual punch entries) ───
    c.execute("""CREATE TABLE IF NOT EXISTS punch_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_code TEXT NOT NULL,
        punch_datetime TEXT NOT NULL,
        punch_type TEXT DEFAULT 'IN',
        source TEXT DEFAULT 'Manual',
        added_by TEXT DEFAULT 'Admin',
        added_on TEXT,
        remarks TEXT,
        UNIQUE(emp_code, punch_datetime, punch_type))""")
    # ── Gratuity & Bonus Settings ────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS gratuity_settings (
        id INTEGER PRIMARY KEY DEFAULT 1,
        formula TEXT DEFAULT 'last_basic_26',
        min_years REAL DEFAULT 5.0,
        rate_per_year REAL DEFAULT 15.0,
        days_divisor INTEGER DEFAULT 26,
        taxable_limit REAL DEFAULT 2000000,
        include_hra INTEGER DEFAULT 0,
        include_special INTEGER DEFAULT 0,
        notes TEXT DEFAULT 'As per Payment of Gratuity Act 1972',
        updated_on TEXT)""")
    c.execute("INSERT OR IGNORE INTO gratuity_settings (id) VALUES (1)")
    c.execute("""CREATE TABLE IF NOT EXISTS bonus_settings (
        id INTEGER PRIMARY KEY DEFAULT 1,
        formula TEXT DEFAULT 'statutory',
        min_rate_pct REAL DEFAULT 8.33,
        max_rate_pct REAL DEFAULT 20.0,
        wage_ceiling REAL DEFAULT 21000,
        calculation_base TEXT DEFAULT 'basic_hra',
        bonus_ceiling_wages REAL DEFAULT 7000,
        min_working_days INTEGER DEFAULT 30,
        applicable_category TEXT DEFAULT 'All',
        notes TEXT DEFAULT 'As per Payment of Bonus Act 1965',
        updated_on TEXT)""")
    c.execute("INSERT OR IGNORE INTO bonus_settings (id) VALUES (1)")
    c.execute("""CREATE TABLE IF NOT EXISTS gratuity_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_code TEXT NOT NULL,
        emp_name TEXT,
        department TEXT,
        date_of_joining TEXT,
        date_of_exit TEXT,
        years_of_service REAL,
        last_basic REAL,
        last_hra REAL,
        gratuity_amount REAL,
        tax_exempt REAL,
        taxable REAL,
        status TEXT DEFAULT 'Calculated',
        calculated_on TEXT,
        remarks TEXT)""")
    c.execute("""CREATE TABLE IF NOT EXISTS bonus_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_code TEXT NOT NULL,
        emp_name TEXT,
        department TEXT,
        year INTEGER,
        months_worked INTEGER DEFAULT 12,
        basic_wages REAL,
        bonus_rate_pct REAL,
        bonus_amount REAL,
        status TEXT DEFAULT 'Pending',
        calculated_on TEXT,
        remarks TEXT)""")
    # ── Gratuity Encashment ───────────────────────────────────
    try: c.execute("ALTER TABLE gratuity_records ADD COLUMN encashed INTEGER DEFAULT 0")
    except: pass
    try: c.execute("ALTER TABLE gratuity_records ADD COLUMN encashed_on TEXT")
    except: pass
    try: c.execute("ALTER TABLE gratuity_records ADD COLUMN encashed_by TEXT")
    except: pass
    # ── Leave Earn Log ──────────────────────────────────────────
    c.execute("""CREATE TABLE IF NOT EXISTS leave_earn_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_code TEXT NOT NULL,
        year INTEGER NOT NULL,
        month INTEGER NOT NULL,
        el_credited REAL DEFAULT 0,
        cl_credited REAL DEFAULT 0,
        credited_on TEXT,
        source TEXT DEFAULT 'auto',
        UNIQUE(emp_code, year, month))""")
    c.execute("""CREATE TABLE IF NOT EXISTS emp_dept_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_code TEXT NOT NULL,
        emp_name TEXT,
        old_department TEXT,
        new_department TEXT,
        changed_on TEXT NOT NULL,
        changed_by TEXT DEFAULT 'Admin',
        FOREIGN KEY(emp_code) REFERENCES employees(emp_code))""")

    # Default machine
    # No default machine — user will add manually

    # Pre-load Vijayshri Packaging company shifts (eSSL exact fields)
    c.execute("SELECT COUNT(*) FROM shifts")
    if c.fetchone()[0] == 0:
        # name,code,cat,start,end,is_next_day,wh,grace,ot_formula,punch_begin_before,ai_start,ai_end
        shifts_data = [
          # name               code     cat         start   end     nxt  wh    gr  otf                    pbb  ai_s    ai_e
          ("General",          "GS",   "Associate","08:00","16:30", 0,  8.5,  15, "total_minus_shift",    90, "06:00","11:00"),
          ("Night Shift",      "NS",   "Associate","18:30","03:00", 1,  8.5,  15, "total_minus_shift",    90, "17:00","22:00"),
          ("Canteen Day",      "CDAY", "Associate","07:45","16:15", 0,  8.5,  15, "total_minus_shift",    90, "05:45","10:30"),
          ("Canteen 2nd Shift","CNIGHT","Associate","13:00","21:30", 0,  8.5,  15, "total_minus_shift",    90, "11:00","15:30"),
          ("Canteen 12HRS",    "CAN12","Associate","06:00","23:59", 0, 12.0,   0, "total_minus_shift",    90, "04:00","08:00"),
          ("Pantry",           "PANTRY","Associate","07:00","23:59", 0,  8.5,  15, "total_minus_shift",    90, "05:00","09:30"),
          ("Security Day",     "SECDAY","Associate","07:00","15:30", 0,  8.5,  15, "total_minus_shift",    90, "05:00","09:30"),
          ("Security Night",   "SECNS","Associate","19:00","03:30", 1,  8.5,  15, "total_minus_shift",    90, "17:00","22:00"),
          ("Staff Day",        "SDAY", "Staff",    "09:30","19:30", 0, 10.0,  15, "out_minus_shift_end",  90, "07:30","11:30"),
          ("Staff Female Shift","SFS", "Staff",    "09:30","18:00", 0,  8.5,  15, "out_minus_shift_end",  90, "07:30","11:30"),
          ("Staff Female 10:00","SFS10","Staff",   "10:00","18:30", 0,  8.5,  15, "out_minus_shift_end",  90, "08:00","12:00"),
          ("Staff General 10:00","S10","Staff",    "10:00","20:00", 0, 10.0,  15, "out_minus_shift_end",  90, "08:00","12:00"),
          ("Staff NS",         "SNS",  "Staff",    "20:00","06:00", 1, 10.0,  15, "out_minus_shift_end",  90, "18:00","22:00"),
        ]
        for nm,cd,cat,st,et,nxt,wh,gr,otf,pbb,ai_s,ai_e in shifts_data:
            is_ngt = 1 if nxt else (1 if t2m(et) is not None and t2m(st) is not None and t2m(et) < t2m(st) else 0)
            conn.execute("""INSERT OR IGNORE INTO shifts
               (shift_name,shift_code,category,start_time,end_time,is_next_day,
                working_hours,grace_minutes,ot_formula,punch_begin_before,
                allowed_in_start,allowed_in_end,is_night_shift,is_active,created_on)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1,date('now'))""",
               (nm,cd,cat,st,et,nxt,wh,gr,otf,pbb,ai_s,ai_e,is_ngt))
    else:
        # Migrate: set allowed_in for shifts missing it
        conn.execute("""UPDATE shifts SET
            allowed_in_start=time(start_time,'-2 hours'),
            allowed_in_end=time(start_time,'+3 hours')
            WHERE (allowed_in_start IS NULL OR allowed_in_start='')
            AND start_time IS NOT NULL""")
        # Sync is_next_day with is_night_shift for existing records
        conn.execute("""UPDATE shifts SET is_next_day=is_night_shift
            WHERE is_next_day IS NULL OR is_next_day=''""")

    # ── Performance indexes ──────────────────────────────────
    for idx_sql in [
        "CREATE INDEX IF NOT EXISTS idx_att_emp_date ON attendance(emp_code, att_date)",
        "CREATE INDEX IF NOT EXISTS idx_att_date ON attendance(att_date)",
        "CREATE INDEX IF NOT EXISTS idx_att_emp ON attendance(emp_code)",
        "CREATE INDEX IF NOT EXISTS idx_lb_emp_year ON leave_balance(emp_code, year)",
        "CREATE INDEX IF NOT EXISTS idx_custom_ded_emp ON custom_deductions(emp_code, status)",
        "CREATE INDEX IF NOT EXISTS idx_users_emp ON users(emp_id)",
        "CREATE INDEX IF NOT EXISTS idx_perm_user ON user_permissions(user_id)",
        "CREATE INDEX IF NOT EXISTS idx_sal_emp ON salary_records(emp_code, month, year)",
        "CREATE INDEX IF NOT EXISTS idx_roster_emp_date ON shift_roster_dates(emp_code, shift_date)",
        "CREATE INDEX IF NOT EXISTS idx_emp_status ON employees(status)",
        "CREATE INDEX IF NOT EXISTS idx_emp_code ON employees(emp_code)",
    ]:
        try: c.execute(idx_sql)
        except: pass

    conn.commit(); conn.close()

# ─────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────
def t2m(t):
    if not t: return None
    try: h,m=map(int,str(t).strip().split(":")); return h*60+m
    except: return None


def find_emp_by_machine_id(conn, machine_uid):
    """
    Robust employee code matching for biometric machine import.
    Matches ALL employees (active + inactive) so no logs are missed.
    Active/Inactive filtering is done at display/report level only.
    
    Handles all format variations:
      Machine: "35"    Software: "0035" ✅
      Machine: "0035"  Software: "35"   ✅  
      Machine: "1001"  Software: "1001" ✅
      Machine: "5"     Software: "0005" ✅
    Returns (emp_row, matched_code) or (None, None)
    """
    uid = str(machine_uid).strip()
    if not uid: return None, None

    # All format variations
    candidates = []
    seen = set()
    for c in [
        uid,
        uid.zfill(4),
        uid.zfill(3),
        uid.lstrip("0") or "0",
        uid.lstrip("0").zfill(4),
        str(int(uid)) if uid.isdigit() else uid,
    ]:
        if c not in seen:
            seen.add(c)
            candidates.append(c)

    for code in candidates:
        # Match ANY employee - active OR inactive - no logs should be missed
        emp = conn.execute(
            "SELECT emp_code, category, status FROM employees WHERE emp_code=?",
            (code,)
        ).fetchone()
        if emp:
            return emp, code

    return None, None


def get_ss():
    """Get salary settings from DB"""
    conn=get_db()
    s=conn.execute("SELECT * FROM salary_settings WHERE id=1").fetchone()
    conn.close()
    if s: return dict(s)
    return {"hra_pct":40,"special_pct":20,"pf_pct":12,"esi_pct":0.75,"esi_limit":21000,
            "ot_days_divisor":26,"ot_hours_divisor":8,"late_grace_minutes":15,"short_time_limit_hrs":5}

def t2m_safe(t):
    if not t: return None
    try:
        p=str(t).strip().split(":")
        return int(p[0])*60+int(p[1])
    except: return None

def apply_night_merge(daily_dict, conn=None):
    """
    Night-shift next-day OUT merge — roster-aware.

    For each consecutive day pair (Day N, Day N+1):
      1. Look up Day N shift in shift_roster_dates (emp_code, date)
      2. If roster says Night Shift → merge Day N+1 early punch (<09:00) as Day N OUT
      3. If no roster entry → use heuristic: last punch >= 17:00 = night shift
      4. If Day N already has 2+ punches (has both IN and OUT) → skip merge entirely

    This correctly handles:
      - Week 1 Day shift, Week 2 Night shift, Week 3 Day shift etc.
      - Month boundary (1st day early punch NOT stolen if prev month had no roster night)
      - Day+Night same employee (roster decides per day)

    daily_dict: { (uid_str, 'YYYY-MM-DD'): [sorted time strings] }
    conn: optional DB connection for roster lookup (creates own if None)
    """
    from datetime import datetime as _dtt2
    def _tm2(s):
        try: h,m=map(int,str(s).split(":")[:2]); return h*60+m
        except: return -1

    # Build roster cache: {(emp_code, date): is_night_shift bool}
    roster_cache = {}
    if conn:
        try:
            rows = conn.execute("""SELECT srd.emp_code, srd.shift_date, s.is_night_shift, s.start_time
                FROM shift_roster_dates srd JOIN shifts s ON srd.shift_id=s.id""").fetchall()
            for r in rows:
                is_night = int(r["is_night_shift"] or 0) == 1
                start_m  = _tm2(r["start_time"] or "")
                # Also treat shifts starting >= 14:00 as night even if flag not set
                roster_cache[(str(r["emp_code"]), str(r["shift_date"]))] = is_night or start_m >= 14*60
        except: pass

    merged = dict(daily_dict)
    all_uids = sorted(set(u for u,_ in daily_dict.keys()))
    for uid in all_uids:
        dates = sorted(d for u,d in daily_dict.keys() if u == uid)
        for i in range(1, len(dates)):
            prev_d = dates[i-1]
            curr_d = dates[i]
            try:
                diff = (_dtt2.strptime(curr_d,"%Y-%m-%d") - _dtt2.strptime(prev_d,"%Y-%m-%d")).days
                if diff != 1: continue
            except: continue

            prev_times = merged.get((uid, prev_d), [])
            curr_times = merged.get((uid, curr_d), [])
            if not prev_times or not curr_times: continue

            # If prev day already has 2+ punches → it has IN+OUT, skip merge
            if len(prev_times) >= 2:
                continue

            last_prev_m  = max(_tm2(t) for t in prev_times)
            first_curr_m = min((_tm2(t) for t in curr_times if _tm2(t) >= 0), default=-1)
            if first_curr_m < 0 or first_curr_m >= 9*60: continue  # curr not early

            # Determine if prev day is night shift
            roster_key = (uid, prev_d)
            if roster_key in roster_cache:
                is_prev_night = roster_cache[roster_key]
            else:
                # Heuristic fallback: last punch >= 17:00 suggests night shift
                is_prev_night = last_prev_m >= 17*60

            if not is_prev_night:
                continue  # Day shift — do NOT steal next day's early punch

            early_punches = [t for t in curr_times if _tm2(t) >= 0 and _tm2(t) < 9*60]
            late_punches  = [t for t in curr_times if _tm2(t) >= 9*60]

            # Append night OUT to prev day
            merged[(uid, prev_d)] = prev_times + early_punches

            if late_punches:
                merged[(uid, curr_d)] = late_punches
            else:
                merged.pop((uid, curr_d), None)

    return merged


def _get_shift_windows(shift):
    """
    Shift Window Based Pairing — VPL PayRoll v1.8
    ═══════════════════════════════════════════════
    Returns IN and OUT windows in minutes-from-midnight.
    OUT window covers Super OT (extends well past shift end).
    Values > 1440 = next day.

    Day/Evening Shift (e.g. 08:00-16:30, 09:30-19:30):
      IN  window : shift_start - 2hrs  →  shift_start + 4hrs
      OUT window : shift_start + 4hrs  →  shift_end + 10hrs (Super OT until ~02:30)

    Night/Cross-Midnight Shift (e.g. 19:00-03:30, 20:00-04:00):
      IN  window : shift_start - 2hrs  →  shift_start + 4hrs
      OUT window : 1440 + shift_end - 2hrs  →  1440 + shift_end + 6hrs (Super OT)

    Logic:
      - IN window ends at shift_start + 4hrs (very late arrival still captured)
      - OUT window starts where IN window ends (no overlap = no confusion)
      - OUT window extends 10hrs after shift_end for day shifts (covers midnight Super OT)
      - OUT window extends 6hrs after shift_end for night shifts (covers morning Super OT)
    """
    def _m(s):
        try: h,mm=map(int,str(s).strip().split(":")[:2]); return h*60+mm
        except: return -1

    st       = _m(shift.get("start_time","08:00"))
    et       = _m(shift.get("end_time","16:30"))
    is_night = int(shift.get("is_night_shift",0) or 0)

    if st < 0: st = 8*60
    if et < 0: et = 16*60+30

    # IN window: 2hrs before shift start → 4hrs after shift start
    in_win_s = max(0, st - 120)   # 2 hrs before
    in_win_e = st + 240           # 4 hrs after start (captures very late arrivals)

    if is_night or (et < st):
        # ── Night / Cross-Midnight Shift ──────────────────────────
        # e.g. SECNS 19:00-03:30
        #
        # IN  window: 17:00 - 23:59 (shift_start - 2hrs to midnight)
        #   → strictly same day, before midnight only
        #   → 08:08 next morning will NOT match IN window → correctly ignored
        #
        # OUT window: next day 00:00 - shift_end + 4hrs (Super OT)
        #   → e.g. 03:30 end → OUT window 00:00 to 07:30
        #   → 08:08 next day → OUTSIDE OUT window → correctly ignored
        #   → 03:28 next day → inside OUT window ✅
        #
        # This prevents morning punches (08:00+) from being mistaken as
        # cross-midnight OUT punches for night shift employees.

        in_win_s  = max(0, st - 120)    # 2 hrs before shift start
        in_win_e  = 1439                 # up to 23:59 same day (not crossing midnight)

        out_win_s = 1440 + 0             # next day 00:00
        out_win_e = 1440 + et + 240      # next day shift_end + 4hrs Super OT
        # e.g. 03:30 + 4hrs = 07:30 → 08:08 is outside → ✅ not mistaken as OUT

    else:
        # ── Day / Evening Shift ───────────────────────────────────
        # OUT window starts after IN window, extends for Super OT
        # e.g. GS 08:00-16:30 → OUT window 12:00 - 02:30 next day
        out_win_s = in_win_e             # starts after IN window ends (no overlap)
        out_win_e = et + 600             # shift_end + 10hrs Super OT
        # Values > 1440 = next-day punches (handled in punch_in_window)

    return in_win_s, in_win_e, out_win_s, out_win_e


def etimetrack_pair_punches(all_punches_sorted, shifts_list, category="Associate",
                             roster_map=None):
    """
    Shift Window Based Punch Pairing — VPL PayRoll v1.8
    ════════════════════════════════════════════════════

    Algorithm (per employee, full date range):
      1. Sort ALL punches chronologically
      2. Deduplicate: gap < 5 min = same event, skip
      3. Group by calendar date
      4. For each date:
         a. Get assigned shift from roster_map (Roster → Fixed → None)
         b. Classify each punch into IN window or OUT window
         c. First valid IN window punch = IN
         d. Last valid OUT window punch = OUT
         e. If OUT is next-day punch → attach to today's record (cross-midnight)
         f. Punch outside both windows → discard (double punch / stray)
         g. If no shift assigned → fallback to sequential pairing (old logic)
      5. Result: clean IN/OUT pairs, punches never go idhar-udhar

    Super OT:
      OUT window extends 6 hrs after shift end (day shift)
      OUT window extends 3 hrs after shift end next day (night shift)
      → Cross-midnight Super OT automatically handled

    roster_map: dict of (emp_code, date_str) → shift_dict
                Passed from caller (ZK import / recalculate)
    """
    if not all_punches_sorted:
        return []

    # Step 1: Sort
    punches = sorted(all_punches_sorted)

    # Step 2: Deduplicate — gap < 5 min = same event
    # Also: after a valid OUT punch, if next punch comes within
    # 30 minutes AND it's the same day → treat as stray/double punch
    # (e.g. employee punched OUT at gate, then again at cabin)
    deduped = [punches[0]]
    for p in punches[1:]:
        gap_min = (p - deduped[-1]).total_seconds() / 60
        if gap_min < 10:
            continue  # duplicate — 10-min machine setting (face-detection multi-punch)
        # No extra post-OUT guard — 10-min dedup is sufficient.
        # Reason: shift changes can mean employee OUT at 06:52 and IN at 07:30
        # so any guard > 10 min would incorrectly drop legitimate IN punches.
        # Face detection duplicates (1-2 sec apart) are already caught above.
        deduped.append(p)

    if not deduped:
        return []

    # ══════════════════════════════════════════════════════════════════
    # ORIGINAL SEQUENTIAL PAIRING LOGIC — Restored
    # ══════════════════════════════════════════════════════════════════
    # Rules (from original concept doc):
    #   Punch 1 = IN, Punch 2 = OUT, Punch 3 = next IN, Punch 4 = OUT...
    #   Duplicate: gap < 5 min → already removed in dedup step
    #   Night shift cross-midnight: OUT time < IN time → next day OUT
    #   Missing OUT: shift end time used as OUT (handled in save_att_row)
    #   Max gap: Staff=14hrs, Associate=23hrs
    #
    # Window-based pairing REMOVED — caused incorrect pairing for
    # night shift employees when morning punches fell in OUT window.
    # ══════════════════════════════════════════════════════════════════

    import datetime as _dt_mod

    # Group all punches chronologically — no date grouping
    # Process as a flat stream: odd index=IN, even index=OUT
    records      = []
    i            = 0
    max_gap_min  = 14 * 60 if category == "Staff" else 23 * 60

    while i < len(deduped):
        in_punch = deduped[i]
        i += 1

        out_punch = None

        # Look for OUT punch
        if i < len(deduped):
            candidate = deduped[i]
            gap_min   = (candidate - in_punch).total_seconds() / 60

            # Accept as OUT if within max gap
            if 0 < gap_min <= max_gap_min:
                out_punch = candidate
                i += 1
            # else: candidate is too far away or negative gap
            # → treat as next day's IN, leave out_punch = None

        # Determine attendance date = IN punch date
        att_date = in_punch.date()

        in_time  = in_punch.strftime("%H:%M")
        out_time = out_punch.strftime("%H:%M") if out_punch else ""
        worked   = 0

        if out_punch:
            worked = int((out_punch - in_punch).total_seconds() / 60)
            worked = max(0, min(worked, 24 * 60))

        # Cross-month guard: if this IN punch is on a NEW month compared to
        # previous record's date, and it falls before 12:00, it is likely the
        # orphaned OUT of the previous month's last night shift that got stored
        # on month+1 day-1.  Skip it as an IN — it has no valid OUT anyway.
        if records:
            import calendar as _cal
            prev_date = _dt_mod.date.fromisoformat(records[-1]["date"])
            # Check if in_punch crossed into a new month
            if (in_punch.date().year, in_punch.date().month) != (prev_date.year, prev_date.month):
                # It's the first punch of a new month
                # If it falls before noon AND no out_punch — it is an orphan OUT punch
                # that auto-import stored on the new month's date. Skip it.
                if in_punch.hour < 12 and out_punch is None:
                    continue  # orphan cross-month OUT — skip, don't create Absent record

        records.append({
            "date":           att_date.strftime("%Y-%m-%d"),
            "in_time":        in_time,
            "out_time":       out_time,
            "shift":          None,
            "shift_name":     "",
            "worked_minutes": worked,
            "ot_minutes":     0,
        })
    return records


def get_all_shifts(conn):
    """Return all active shifts as list of dicts."""
    rows = conn.execute("SELECT * FROM shifts WHERE is_active=1 ORDER BY shift_name").fetchall()
    return [dict(r) for r in rows]


def get_shift_for_emp(emp_code, for_date=None, conn=None):
    """
    eSSL eTimeTrackLite — 3-mode shift lookup per employee per date.

    Mode priority:
      1. ROSTER  — shift_roster_dates has entry for this date
      2. FIXED   — employee_shifts.shift_mode='fixed' → always use that shift
      3. AUTO    — no assignment → caller must detect from punch time

    Returns (shift_dict, mode) or (None, 'auto')
    """
    close_conn = False
    if conn is None:
        conn = get_db(); close_conn = True
    try:
        check_date = for_date or date.today().strftime("%Y-%m-%d")

        # Priority 1: Roster (date-specific assignment)
        s = conn.execute("""SELECT s.* FROM shifts s
            JOIN shift_roster_dates srd ON s.id=srd.shift_id
            WHERE srd.emp_code=? AND srd.shift_date=? AND s.is_active=1""",
            (emp_code, check_date)).fetchone()
        if s:
            return dict(s), 'roster'

        # Priority 2: Employee's assigned shift
        es = conn.execute("""SELECT es.shift_mode, s.* FROM employee_shifts es
            JOIN shifts s ON es.shift_id=s.id
            WHERE es.emp_code=? AND s.is_active=1""", (emp_code,)).fetchone()
        if es:
            mode = es["shift_mode"] or "fixed"
            shift_dict = dict(es)
            # 'fixed' mode → always use this shift
            # 'auto' mode → return as reference but caller can override with punch detection
            if mode == "fixed":
                return shift_dict, "fixed"
            else:
                # auto mode: return shift as hint, caller uses punch detection
                return shift_dict, "auto"

        # Priority 3: Auto detect (caller handles punch-time detection)
        return None, 'auto'
    finally:
        if close_conn: conn.close()


def detect_shift_by_punch(in_time_str, shifts_list):
    """
    eSSL eTimeTrackLite — Auto Shift Detection.

    eSSL exact algorithm:
      1. Check each shift's allowed_in_start → allowed_in_end window
         If IN punch falls in window → candidate
      2. Among candidates, pick nearest shift begin_time
      3. If no window match → pick globally nearest begin_time (fallback)

    in_time_str : 'HH:MM' string
    shifts_list : list of shift dicts
    Returns shift dict or None
    """
    if not shifts_list or not in_time_str:
        return None

    def _m(s):
        try: h,mm=map(int,str(s).strip().split(':')[:2]); return h*60+mm
        except: return -1

    pin = _m(in_time_str)
    if pin < 0:
        return None

    def in_window(shift, pin):
        ai_s = _m(shift.get('allowed_in_start',''))
        ai_e = _m(shift.get('allowed_in_end',''))
        if ai_s < 0 or ai_e < 0:
            # No window defined — use punch_begin_before as window
            pbb = int(shift.get('punch_begin_before') or 60)
            st  = _m(shift.get('start_time',''))
            if st < 0: return False
            win_s = (st - pbb) % (24*60)
            win_e = (st + 3*60) % (24*60)
            ai_s, ai_e = win_s, win_e
        if ai_e >= ai_s:
            return ai_s <= pin <= ai_e
        else:  # wraps midnight
            return pin >= ai_s or pin <= ai_e

    # Step 1: Window match candidates
    candidates = [s for s in shifts_list if in_window(s, pin)]

    pool = candidates if candidates else shifts_list  # fallback = all

    # Step 2: Nearest begin_time
    best = None; best_diff = 9999
    for s in pool:
        st = _m(s.get('start_time',''))
        if st < 0: continue
        diff = abs(pin - st)
        if diff > 12*60: diff = 24*60 - diff  # circular
        if diff < best_diff:
            best_diff = diff; best = s
    return best

def is_holiday_date(d, category="All"):
    """Check if date is a holiday for given category (Staff/Associate/All)"""
    conn=get_db()
    h=conn.execute("""SELECT id FROM holidays WHERE holiday_date=?
        AND (applies_to='All' OR applies_to IS NULL OR applies_to='' OR applies_to=? OR applies_to='Staff' OR applies_to='Associate')""",
        (d, category)).fetchone()
    conn.close()
    return h is not None

def is_sunday_date(d):
    try:
        from datetime import datetime as dt2
        return dt2.strptime(d,"%Y-%m-%d").weekday()==6
    except: return False

_WO_DAY_MAP = {"Monday":0,"Tuesday":1,"Wednesday":2,"Thursday":3,"Friday":4,"Saturday":5,"Sunday":6}

def get_user_dept_filter(extra_alias="e"):
    """Return SQL WHERE clause fragment to filter by user dept_access."""
    try:
        dept_access = session.get("dept_access", [])
        role = session.get("role","")
        if role in ("admin","hr","director","manager") or not dept_access:
            return "", []
        placeholders = ",".join(["?"]*len(dept_access))
        return f" AND {extra_alias}.department IN ({placeholders})", list(dept_access)
    except:
        return "", []

def get_user_depts(conn=None):
    """Return list of allowed departments for current user. Empty = all."""
    try:
        dept_access = session.get("dept_access", [])
        role = session.get("role","")
        if role in ("admin","hr","director","manager") or not dept_access:
            return []  # all
        return dept_access
    except:
        return []

def get_emp_weekly_off_num(emp_code, conn=None):
    """Return weekday number (0=Mon..6=Sun) for employee's weekly off day."""
    close = False
    if conn is None:
        conn = get_db(); close = True
    try:
        row = conn.execute("SELECT weekly_off FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
        day = (row["weekly_off"] if row and row["weekly_off"] else "Sunday")
        return _WO_DAY_MAP.get(day, 6)
    except: return 6
    finally:
        if close: conn.close()

def is_weekly_off_date(d, emp_code, conn=None):
    """Check if given date is the employee's weekly off day."""
    try:
        from datetime import datetime as dt2
        weekday = dt2.strptime(d, "%Y-%m-%d").weekday()
        return weekday == get_emp_weekly_off_num(emp_code, conn)
    except: return False

def calc_att(emp_code, in_t, out_t, category, shift=None, status_override=None):
    """
    eSSL eTimeTrackLite — Exact Attendance Calculation Engine

    Fields used from shift:
      start_time, end_time, is_next_day, working_hours, grace_minutes,
      ot_formula, half_day_minutes, neglect_last_in

    OT Formulas (eSSL exact):
      'out_minus_shift_end'  : OT = OUT - Shift End  (if OUT > Shift End)
      'total_minus_shift'    : OT = Worked - Shift Duration
      'not_applicable'       : OT = 0

    Status override: WOP / Holiday → full duration = OT (WOP/Holiday OT)
    """
    r = {
        "status":          "Present",
        "late_minutes":    0,
        "short_minutes":   0,
        "ot_minutes":      0,
        "is_half_day":     0,
        "working_minutes": 0,
        "is_night_shift":  0,
    }

    if not in_t:
        # OUT punch exists but IN is missing → Miss Punch (not Absent)
        # Absent is only set when BOTH in_t and out_t are empty (no punch at all)
        if out_t:
            r["status"] = "Miss Punch"
            r["punch_miss"] = 1
        else:
            r["status"] = "Absent"
        return r

    def _m(s):
        try: h,mm=map(int,str(s).strip().split(':')[:2]); return h*60+mm
        except: return None

    pin = _m(in_t)
    if pin is None:
        r["status"] = "Absent"; return r

    pout = _m(out_t) if out_t else None

    # ── Shift parameters ────────────────────────────────────────
    if shift:
        sh_start   = _m(shift.get("start_time","")) or 0
        sh_end     = _m(shift.get("end_time",""))   or 0
        is_next_day= int(shift.get("is_next_day") or shift.get("is_night_shift") or 0)
        # Use shift's working_hours for OT base
        _sh_wh = float(shift.get("working_hours") or 8.5)
        if _sh_wh < 1.0 or _sh_wh > 24.0:
            _sh_wh = 8.5  # fallback for invalid values
        wh_min = int(_sh_wh * 60)
        grace      = int(shift.get("grace_minutes") or 15)
        ot_formula = str(shift.get("ot_formula") or "total_minus_shift")
        half_day_m = int(shift.get("half_day_minutes") or 240)
        neglect_li = int(shift.get("neglect_last_in") or 0)
    else:
        # No shift assigned — use standard defaults
        sh_start   = 8*60+30 if category=="Staff" else 8*60
        sh_end     = 17*60+0 if category=="Staff" else 16*60+30
        is_next_day= 0
        wh_min     = 510  # 8:30 hrs standard
        grace      = 15
        ot_formula = "total_minus_shift"
        half_day_m = 240
        neglect_li = 0

    # Night shift end is next day
    if is_next_day and sh_end < sh_start:
        sh_end_adj = sh_end + 24*60
    else:
        sh_end_adj = sh_end

    # ── Status override: WOP / Holiday ──────────────────────────
    if status_override in ("WOP", "Holiday"):
        r["status"] = status_override
        if pout is not None:
            if pout < pin: pout += 24*60  # cross midnight
            worked = min(pout - pin, 24*60)
            # Deduct 30 min break for WOP/Holiday
            worked_after_break = max(0, worked - 30)
            r["working_minutes"] = worked
            r["ot_minutes"]      = worked_after_break  # 30 min break deducted
        return r

    # ── No OUT punch handling ────────────────────────────────────
    if pout is None:
        if neglect_li or shift:
            # Shift is known (roster/fixed assigned) → use shift end as OUT
            # for working minutes calculation.
            # Status will be set to "Miss Punch" by caller (save_att_row / importer)
            # so the employee is clearly flagged for HR review.
            pout = sh_end_adj if is_next_day else sh_end
        else:
            # No shift assigned AND no OUT punch → Miss Punch
            # Alert raised separately via punch_alerts
            if pin > sh_start + grace:
                r["late_minutes"] = pin - (sh_start + grace)
            r["status"] = "Miss Punch"
            r["punch_miss"] = 1
            return r

    # ── Cross-midnight adjustment ────────────────────────────────
    if is_next_day:
        # Night shift: add 24hrs if OUT <= IN
        if pout <= pin:
            pout += 24*60
        r["is_night_shift"] = 1
    else:
        # Day shift: OUT < IN means crossed midnight (Super OT / Double Duty)
        if pout < pin:
            pout += 24*60

    # ── Double shift / Super OT: max valid OUT = IN + 23 hrs ────
    # If total duration > 23 hrs, the OUT punch belongs to NEXT day's IN
    # Flag as punch-miss alert for last day
    max_valid_minutes = 23 * 60
    if (pout - pin) > max_valid_minutes:
        # OUT is too far — treat as missing OUT, alert raised
        r["punch_miss"] = 1
        r["status"] = "Present"
        r["ot_minutes"] = 0
        r["working_minutes"] = 0
        r["short_minutes"] = sh_end_adj - pin if pin < sh_end_adj else 0
        return r

    worked = pout - pin

    # ── Break deduction ──────────────────────────────────────────
    # Double shift (Super OT): worked >= 16 hrs → deduct 1 hr break
    # Normal shift: no break deduction in attendance (eSSL style)
    break_deduct = 0
    if worked >= 16 * 60:
        break_deduct = 60  # 1 hour break for double shift/super OT

    worked_net = worked - break_deduct  # net worked after break
    r["working_minutes"] = worked_net

    # ── Non-Staff: < 3 hrs worked = Absent ──────────────────────
    if category == "Associate" and worked_net < 3 * 60 and worked_net > 0:
        r["status"] = "Absent"
        r["working_minutes"] = 0
        r["ot_minutes"] = 0
        return r

    # ── Late coming ──────────────────────────────────────────────
    if pin > sh_start + grace:
        r["late_minutes"] = pin - (sh_start + grace)

    # ── Early going (short) ──────────────────────────────────────
    if pout < sh_end_adj:
        r["short_minutes"] = sh_end_adj - pout

    # ── Half day check — Range based ─────────────────────────────
    # Use shift-defined ranges if available, else fallback to half_day_minutes
    hd_min = int(shift.get("half_day_min_minutes") or 180) if shift else 180
    hd_max = int(shift.get("full_day_min_minutes") or 390) if shift else 390

    if worked_net < hd_min:
        # Below minimum → Absent (for Associate), for Staff → still present but short
        if category == "Associate":
            r["status"] = "Absent"
            r["working_minutes"] = 0
            r["ot_minutes"] = 0
            return r
        # Staff: mark as half day if very short (< hd_min)
        r["is_half_day"] = 1
    elif worked_net < hd_max:
        # In half-day range
        r["is_half_day"] = 1
    # else: full day present (worked_net >= hd_max)

    # ── OT Calculation ──────────────────────────────────────────
    # Formula: OT = worked_net - shift_hours
    # worked_net = total worked minutes after break deduction
    # This is the most accurate method regardless of shift type
    if ot_formula == "not_applicable":
        r["ot_minutes"] = 0
    elif category == "Staff" and ot_formula == "not_applicable":
        r["ot_minutes"] = 0
    else:
        ot = worked_net - wh_min
        # Minimum 30 minutes OT rule: if OT < 30 min, do not count as OT
        r["ot_minutes"] = max(0, ot) if ot >= 30 else 0

    return r


def get_wd(year, month, category, weekly_off="Sunday"):
    """Get working days for month — checks monthly_working_days override first."""
    try:
        conn = get_db()
        row = conn.execute(
            "SELECT staff_days, nonstaff_days FROM monthly_working_days WHERE year=? AND month=?",
            (year, month)).fetchone()
        conn.close()
        if row:
            return row["staff_days"] if category == "Staff" else row["nonstaff_days"]
    except: pass
    # Default: count calendar days (Staff excludes Sundays)
    total = 0
    for d in range(1, calendar.monthrange(year, month)[1]+1):
        if category == "Staff":
            _wo_map = {"Monday":0,"Tuesday":1,"Wednesday":2,"Thursday":3,"Friday":4,"Saturday":5,"Sunday":6}
            if date(year, month, d).weekday() != _wo_map.get(weekly_off, 6): total += 1
        else: total += 1
    return total

def save_att_row(conn, emp_code, att_date, in_t, out_t, category, status="Present", status_override=None, pre_shift=None, late_count_map=None):
    """
    Roster-Based Attendance Engine — VPL PayRoll v1.8
    Shift is resolved ONLY from Shift Roster (date-specific) or Fixed assignment.
    Auto-Shift detection fully removed — shift MUST be explicitly assigned.
    pre_shift: pre-resolved shift dict (avoids inner DB query if provided)
    """
    # ── Step 1: Get shift (Roster → Fixed only; NO auto-detect) ──────────
    if pre_shift:
        shift = pre_shift
    elif conn is None:
        shift = None  # no DB available, use defaults in calc_att
    else:
        shift, mode = get_shift_for_emp(emp_code, for_date=att_date, conn=conn)
        # ── AUTO-SHIFT DETECTION REMOVED ────────────────────────────────
        # Only 'roster' and 'fixed' modes honoured.
        # mode='auto' (no manual assignment) → shift = None → calc_att
        # uses 8-hr default safely. Employee appears on "Missing Shift"
        # list in Shift Roster page for HR to fix.
        if mode == 'auto':
            shift = None  # do NOT call detect_shift_by_punch

    # ── Step 2: Calculate attendance ──────────────────────────
    c = calc_att(emp_code, in_t, out_t, category, shift=shift)

    if status in ("Absent","Leave","WO"):
        c["status"] = status
    elif status in ("WOP","Holiday"):
        c = calc_att(emp_code, in_t, out_t, category, shift=shift, status_override=status)

    # ── Step 3: Late grace tracking (Staff only) ───────────────
    is_hd = c["is_half_day"]
    if category == "Staff" and c["late_minutes"] > 0:
        mn = int(att_date[5:7]); yr = int(att_date[:4])
        if late_count_map is not None:
            # Use preloaded map — zero DB queries
            late_count = late_count_map.get((emp_code, mn, yr), 0)
        elif conn is not None:
            late_count = conn.execute("""SELECT COUNT(*) FROM attendance
                WHERE emp_code=? AND strftime('%m',att_date)=? AND strftime('%Y',att_date)=?
                AND late_minutes>0""",
                (emp_code, f"{mn:02d}", str(yr))).fetchone()[0]
        else:
            late_count = 0
        if late_count >= 2:
            is_hd = 1

    shift_name = shift["shift_name"] if shift else ""

    # ── Step 4: Upsert attendance ──────────────────────────────
    # Check if late was previously waived — preserve that permanently
    _late_waived = 0
    if conn is not None:
        try:
            _existing = conn.execute(
                "SELECT late_waived, late_minutes, is_manual FROM attendance WHERE emp_code=? AND att_date=?",
                (emp_code, att_date)).fetchone()
            if _existing and (_existing["late_waived"] or 0) == 1:
                _late_waived = 1
                c["late_minutes"] = 0  # Always keep late as 0 if waived — no exceptions
        except: pass

    row_params = (emp_code, att_date,
         in_t or "", out_t or "",
         c["working_minutes"], c["status"],
         c["late_minutes"], c["short_minutes"],
         c["ot_minutes"], is_hd, shift_name)
    if conn is not None:
        # Check if existing record is manually edited — preserve is_manual flag
        _is_manual_preserve = 0
        try:
            _manual_check = conn.execute(
                "SELECT is_manual FROM attendance WHERE emp_code=? AND att_date=?",
                (emp_code, att_date)).fetchone()
            if _manual_check and (_manual_check["is_manual"] or 0) == 1:
                _is_manual_preserve = 1
        except: pass

        if _is_manual_preserve == 1:
            # ── Manual-entry protected record ─────────────────────────
            # This block runs ONLY when save_att_row is called by the
            # manual-entry route (attendance_manual_add / bulk).
            # Reimport and cascade already skip is_manual=1 dates before
            # calling save_att_row, so they never reach here.
            #
            # Priority rule:
            #   • Caller supplied in_t / out_t  → use those (user is
            #     explicitly correcting the record)
            #   • Caller left them blank (None/"") → fall back to whatever
            #     is already stored (partial update: only one side given)
            existing_att = conn.execute(
                "SELECT in_time, out_time FROM attendance WHERE emp_code=? AND att_date=?",
                (emp_code, att_date)).fetchone()
            if existing_att:
                # Caller's value wins; fall back to stored only if caller sent nothing
                final_in  = in_t  if in_t  else (existing_att["in_time"]  or "")
                final_out = out_t if out_t else (existing_att["out_time"] or "")

                # Recalculate with the resolved in/out
                c2 = calc_att(emp_code, final_in or None, final_out or None,
                              category, shift=shift)

                conn.execute("""UPDATE attendance SET
                    in_time=?, out_time=?,
                    working_minutes=?, status=?,
                    late_minutes=CASE WHEN late_waived=1 THEN 0 ELSE ? END,
                    short_minutes=?, ot_minutes=?, is_half_day=?,
                    shift_name=?
                    WHERE emp_code=? AND att_date=?""",
                    (final_in, final_out,
                     c2["working_minutes"], c2["status"],
                     c2["late_minutes"], c2["short_minutes"],
                     c2["ot_minutes"], c2["is_half_day"],
                     shift_name,
                     emp_code, att_date))
            else:
                # No existing record — insert fresh with is_manual=1
                conn.execute("""INSERT INTO attendance
                    (emp_code,att_date,in_time,out_time,working_minutes,status,
                     late_minutes,short_minutes,ot_minutes,is_half_day,shift_name,late_waived,is_manual)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                    row_params + (_late_waived,))
        else:
            conn.execute("""INSERT INTO attendance
                (emp_code,att_date,in_time,out_time,working_minutes,status,
                 late_minutes,short_minutes,ot_minutes,is_half_day,shift_name,late_waived)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(emp_code,att_date) DO UPDATE SET
                in_time=excluded.in_time, out_time=excluded.out_time,
                working_minutes=excluded.working_minutes, status=excluded.status,
                late_minutes=CASE WHEN attendance.late_waived=1 THEN 0 ELSE excluded.late_minutes END,
                short_minutes=excluded.short_minutes,
                ot_minutes=excluded.ot_minutes, is_half_day=excluded.is_half_day,
                shift_name=excluded.shift_name,
                late_waived=CASE WHEN attendance.late_waived=1 THEN 1 ELSE excluded.late_waived END""",
                row_params + (_late_waived,))
    c["_row_params"] = row_params
    return c


def get_payroll_settings(conn):
    """Get payroll settings as dict"""
    s = conn.execute("SELECT * FROM payroll_settings WHERE id=1").fetchone()
    if s: return dict(s)
    return {
        "pf_employer_pct":12.0,"pf_employee_pct":12.0,
        "esic_employer_pct":3.25,"esic_employee_pct":0.75,
        "esic_wage_limit":21000,"pf_wage_limit":15000,
        "pt_state":"Madhya Pradesh","pt_applicable":0,
        "lwf_amount":0,"working_days_month":26,
        "late_grace_minutes":15,"late_halfday_count":3,
        "late_free_days":2,"short_time_limit_hrs":5.0,
        "short_time_per_halfday":2.5,
        "el_per_year":16,"cl_per_year":6
    }

def get_pt_amount(conn, gross, state="Madhya Pradesh"):
    """Get PT deduction based on gross salary and state slab"""
    slab = conn.execute("""SELECT pt_amount FROM pt_slabs
        WHERE state=? AND salary_from<=? AND (salary_to>=? OR salary_to=0)
        AND is_active=1 ORDER BY salary_from DESC LIMIT 1""",
        (state, gross, gross)).fetchone()
    return float(slab["pt_amount"]) if slab else 0.0

def calc_salary_emp(emp_code, month, year, preview=False, force=False):
    """
    Complete Payroll Calculation Engine
    ====================================
    Steps:
    1. Fetch employee + scheme + payroll settings
    2. Fetch attendance (present, absent, WOP, Holiday, leaves, OT, late)
    3. Apply leave rules (Staff vs Non-Staff)
    4. Calculate earnings (Basic, HRA, Special, OT, Bonus, Arrears)
    5. Apply statutory deductions (PF, ESIC, PT, TDS, LWF)
    6. Apply loans/advance deductions
    7. Calculate net salary
    8. Save to salary_records (unless preview mode)
    """
    conn = get_db()
    try:
        # ── Step 1: Employee Data ──────────────────────────────
        emp = conn.execute("SELECT * FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
        if not emp:
            print(f"[PAYROLL] Employee {emp_code} not found in DB")
            return None
        emp = dict(emp)

        # Basic salary check - warn but don't fail
        if not emp.get("basic") or float(emp.get("basic") or 0) == 0:
            print(f"[PAYROLL WARNING] {emp_code}: Basic salary is 0 — salary will be zero")
            # Don't return None - process with zero salary

        # Get scheme
        scheme = None
        if emp.get("scheme_id"):
            scheme = conn.execute("SELECT * FROM employee_schemes WHERE id=?",
                (emp["scheme_id"],)).fetchone()
            if scheme: scheme = dict(scheme)

        # Payroll settings
        ps = get_payroll_settings(conn)

        cat = emp["category"]
        wd  = get_wd(year, month, cat)  # working days in month

        # ── Step 2: Fetch Attendance ───────────────────────────
        att_rows = conn.execute("""SELECT status, ot_minutes, short_minutes,
            is_half_day, late_minutes, late_waived, att_date, in_time, working_minutes
            FROM attendance
            WHERE emp_code=? AND strftime('%m',att_date)=? AND strftime('%Y',att_date)=?""",
            (emp_code, f"{month:02d}", str(year))).fetchall()

        # Leave balance (paid leaves)
        lb = conn.execute("""SELECT cl_used, el_used, cl_allotted, el_allotted
            FROM leave_balance WHERE emp_code=? AND year=?""",
            (emp_code, year)).fetchone()

        # Build att_dict for quick lookup by date
        att_dict = {r["att_date"]: dict(r) for r in att_rows}
        # Note: Zero attendance is OK — will result in zero present days, all absent

        # Build holidays set for this month
        hol_set = set()
        for h in conn.execute("""SELECT holiday_date FROM holidays
            WHERE strftime('%m',holiday_date)=? AND strftime('%Y',holiday_date)=?
            AND (applies_to='All' OR applies_to IS NULL OR applies_to=''
                 OR applies_to=? OR applies_to='Staff' OR applies_to='Associate')""",
            (f"{month:02d}", str(year), cat)).fetchall():
            hol_set.add(h["holiday_date"])

        # ── Iterate every calendar day of month ────────────────
        import calendar as _cal
        total_cal_days = _cal.monthrange(year, month)[1]

        present_days    = 0.0
        absent_days     = 0.0
        wop_days        = 0.0
        holiday_days    = 0.0
        paid_leave_days = 0.0
        half_days       = 0.0
        late_marks      = 0
        ot_minutes      = 0
        short_tot       = 0
        wo_days         = 0  # actual weekly offs (no punch)

        # Get employee weekly off day
        _emp_row = conn.execute("SELECT weekly_off FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
        emp_weekly_off = (_emp_row["weekly_off"] if _emp_row and _emp_row["weekly_off"] else "Sunday")

        for day in range(1, total_cal_days + 1):
            dt_str  = f"{year}-{month:02d}-{day:02d}"
            weekday = date(year, month, day).weekday()  # 6=Sunday
            # Use employee's weekly_off day (0=Monday,...,6=Sunday)
            _wo_day_map = {"Monday":0,"Tuesday":1,"Wednesday":2,"Thursday":3,"Friday":4,"Saturday":5,"Sunday":6}
            _wo_weekday = _wo_day_map.get(emp_weekly_off, 6)
            is_sun  = (weekday == _wo_weekday)
            rec     = att_dict.get(dt_str)
            st      = (rec["status"] if rec else "") or ""
            is_hd   = (rec.get("is_half_day", 0) if rec else 0) or 0

            # Determine effective status
            if not st:
                # No attendance record for this day
                if is_sun:
                    st = "WO"   # Weekly off day with no punch = Week Off
                elif dt_str in hol_set:
                    st = "Holiday"  # Holiday with no punch
                else:
                    # ── Night Shift Guard for Payroll ──────────────────
                    # Agar night shift assigned hai aur shift abhi shuru
                    # nahi hui (aaj ya future) → salary mein count mat karo
                    # (naa present, naa absent — skip)
                    _is_ns_payroll = False
                    _ns_start_m_pr = None
                    try:
                        _ns_pr = conn.execute("""SELECT s.is_night_shift, s.start_time
                            FROM shifts s
                            JOIN shift_roster_dates srd ON s.id=srd.shift_id
                            WHERE srd.emp_code=? AND srd.shift_date=?""",
                            (emp_code, dt_str)).fetchone()
                        if _ns_pr:
                            _sm = t2m_safe(_ns_pr["start_time"] or "")
                            _ns_start_m_pr = _sm
                            _is_ns_payroll = (
                                int(_ns_pr["is_night_shift"] or 0) == 1
                                or (_sm is not None and _sm >= 14 * 60)
                            )
                    except: pass

                    if _is_ns_payroll:
                        from datetime import datetime as _dtnow_pr
                        _today_pr = date.today()
                        _cur_day  = date(year, month, day)
                        _now_m_pr = _dtnow_pr.now().hour * 60 + _dtnow_pr.now().minute
                        _snf = False
                        if _cur_day >= _today_pr:
                            if _cur_day > _today_pr:
                                _snf = True
                            else:
                                _def_st = _ns_start_m_pr if _ns_start_m_pr else 20 * 60
                                _snf = (_now_m_pr < _def_st)
                        if _snf:
                            continue  # Shift abhi shuru nahi — skip (naa present naa absent)
                        else:
                            st = "Absent"  # Past night shift, no punch = genuinely absent
                    else:
                        st = "Absent"   # Working day with no punch = Absent

            # Count by status
            if st == "WO":
                wo_days += 1
                continue  # WO = not counted in working days

            elif st == "WOP":
                # Weekly Off Present — employee worked on day off
                wop_days += 1
                ot_minutes += (rec.get("ot_minutes", 0) if rec else 0) or 0
                # WOP = OT only, NOT a paid working day
                continue

            elif st == "Holiday":
                holiday_days += 1
                ot_minutes += (rec.get("ot_minutes", 0) if rec else 0) or 0
                # Holiday = NOT counted in payable/working days
                # OT on holiday goes to OT process only
                # No impact on salary working days
                continue

            elif st == "Leave":
                # Approved Paid Leave
                if is_hd:
                    # Half day leave: 0.5 leave + 0.5 present (employee was also present half day)
                    paid_leave_days += 0.5
                    present_days    += 0.5
                    half_days       += 1
                else:
                    paid_leave_days += 1
                ot_minutes += (rec.get("ot_minutes", 0) if rec else 0) or 0
                continue

            elif st == "Absent":
                absent_days += 1
                continue

            elif st in ("Present", "Miss Punch"):
                wm_min = (rec.get("working_minutes", 0) if rec else 0) or 0
                # Associate: if worked < 3 hours → treat as Absent (no pay)
                if cat == "Associate" and wm_min > 0 and wm_min < 180:
                    absent_days += 1
                    continue
                if is_hd:
                    present_days += 0.5
                    half_days    += 1
                else:
                    present_days += 1
                ot_minutes += (rec.get("ot_minutes", 0) if rec else 0) or 0
                short_tot  += (rec.get("short_minutes", 0) if rec else 0) or 0
                # Only count late if NOT waived
                if ((rec.get("late_minutes", 0) if rec else 0) or 0) > 0 and \
                   not (rec.get("late_waived", 0) if rec else 0):
                    late_marks += 1
                # Miss Punch: counted as Present for salary, HR notified via Punch Alerts

            else:
                # Any other status (e.g. old data) — treat as Present
                present_days += 0.5 if is_hd else 1
                ot_minutes += (rec.get("ot_minutes", 0) if rec else 0) or 0

        # ── Step 3: Staff Late Mark & Short Time Deduction ────────────
        # Rules:
        # - Grace: 15 min per day (already handled in attendance calc)
        # - First 2 late days per month: FREE (no deduction)
        # - From 3rd late day onwards: each late day = 0.5 day deduction
        # - Short time: total month deficit vs shift hours, 5 hrs allowed
        # - Early coming: counts toward duration (NOT ignored)
        # - Associates: NO deduction (perfect as-is)

        if cat == "Staff":
            # Late deduction: configurable free days, then 0.5 day per late
            _late_free = int(ps.get("late_free_days", 2) or 2)
            if late_marks > _late_free:
                chargeable_lates = late_marks - _late_free
                present_days = max(0, present_days - chargeable_lates * 0.5)

            # Short time deduction: configurable allowance and rate
            _short_limit_hrs = float(ps.get("short_time_limit_hrs", 5.0) or 5.0)
            _short_allow_min = int(_short_limit_hrs * 60)
            _short_per_hd_hrs = float(ps.get("short_time_per_halfday", 2.5) or 2.5)
            _short_per_hd_min = max(1, int(_short_per_hd_hrs * 60))
            if short_tot > _short_allow_min:
                excess_min = short_tot - _short_allow_min
                extra_halfdays = int(excess_min / _short_per_hd_min)
                if extra_halfdays > 0:
                    present_days = max(0, present_days - extra_halfdays * 0.5)

        # Payable days = Present + Paid Leave + Holidays (already in paid_leave_days)
        payable_days = present_days + paid_leave_days

        # ── Step 4: Salary Components ──────────────────────────
        basic = float(emp.get("basic", 0) or 0)
        hra   = float(emp.get("hra", 0) or 0)
        special = float(emp.get("special_allowance", 0) or 0)

        working_days = wd if wd > 0 else 26
        per_day = basic / working_days if working_days else 0
        per_day_salary = round(per_day, 2)  # stored in salary_records

        basic_earned   = round(per_day * payable_days, 2)
        hra_earned     = round((hra / working_days * payable_days) if working_days else 0, 2)
        special_earned = round((special / working_days * payable_days) if working_days else 0, 2)

        # ── Step 5: OT — tracked only, NOT included in salary ──
        # OT is processed separately via OT Process page
        # Salary = Basic + HRA + Special only (no OT)
        ot_hours  = round(ot_minutes / 60, 2)
        ot_amount = 0.0  # OT excluded from salary — use OT Process page

        # PF/ESI: employee checkbox is MASTER — overrides scheme
        # 0 or None = Not applicable, 1 = Applicable
        emp_pf_val  = emp.get("pf_applicable")
        emp_esi_val = emp.get("esi_applicable")
        # Explicit 0 = disabled. None or 1 = enabled.
        emp_pf  = 0 if (emp_pf_val is not None and int(emp_pf_val or 0) == 0) else 1
        emp_esi = 0 if (emp_esi_val is not None and int(emp_esi_val or 0) == 0) else 1
        sch_pf  = 1 if (not scheme) else int(scheme.get("pf_applicable", 1) or 1)
        sch_esi = 1 if (not scheme) else int(scheme.get("esi_applicable", 1) or 1)
        pf_app  = bool(emp_pf and sch_pf)
        esi_app = bool(emp_esi and sch_esi)
        ot_app  = bool(scheme["ot_applicable"]  if scheme else 1)

        # ── Bonus & Arrears ────────────────────────────────────
        bonus_total = conn.execute("""SELECT COALESCE(SUM(amount),0) as s FROM payroll_bonus
            WHERE emp_code=? AND month=? AND year=?""",
            (emp_code, month, year)).fetchone()["s"] or 0

        # ── Gross Earnings ─────────────────────────────────────
        # Gross = Salary components only — OT excluded
        gross = round(basic_earned + hra_earned + special_earned + bonus_total, 2)

        # ── Step 6: Statutory Deductions ──────────────────────
        # PF = Basic Earned Salary × 12%
        # Employee contribution: Basic Earned × 12%
        # Employer contribution: Basic Earned × 12%
        # Both calculated on ACTUAL EARNED Basic (no wage cap)
        pf_pct_emp = float(ps.get("pf_employee_pct", 12)) / 100  # default 12%
        pf_pct_er  = float(ps.get("pf_employer_pct", 12)) / 100  # default 12%
        pf_emp = round(basic_earned * pf_pct_emp, 2) if pf_app else 0
        pf_er  = round(basic_earned * pf_pct_er,  2) if pf_app else 0
        # Example: Basic Earned ₹18,878 × 12% = ₹2,265 (Employee PF)
        #          Basic Earned ₹18,878 × 12% = ₹2,265 (Employer PF)

        # ESIC (0.75% employee, 3.25% employer — only if gross <= 21000)
        esic_limit = float(ps.get("esic_wage_limit", 21000))
        esi_emp = round(gross * (ps.get("esic_employee_pct", 0.75) / 100), 2) if (esi_app and gross <= esic_limit) else 0
        esi_er  = round(gross * (ps.get("esic_employer_pct", 3.25) / 100), 2) if (esi_app and gross <= esic_limit) else 0

        # PT (Professional Tax) — on ACTUAL GROSS (full month salary, not earned)
        # Actual Gross = Basic + HRA + Special (full month, pre-proration)
        actual_gross = round(basic + hra + special, 2)
        pt_app_settings = bool(ps.get("pt_applicable", 0))
        pt_app_scheme   = bool(scheme["pt_applicable"] if scheme else False)
        pt = 0.0
        # PT = 0 if employee has zero present days (no work done this month)
        if present_days > 0 and (pt_app_settings or pt_app_scheme):
            pt = get_pt_amount(conn, actual_gross, ps.get("pt_state", "Madhya Pradesh"))

        # LWF
        lwf_app = bool(scheme["lwf_applicable"] if scheme else 0) if scheme else False
        lwf = float(ps.get("lwf_amount", 0)) if lwf_app else 0

        # TDS — only if manually set on employee (tds_percent > 0)
        # Auto slab NOT applied by default (set tds_percent on employee to deduct)
        tds = 0.0
        if emp.get("tds_percent") and float(emp["tds_percent"] or 0) > 0:
            tds = round(gross * float(emp["tds_percent"]) / 100, 2)

        # ── Step 7: Loan/Advance Deductions ───────────────────
        # NOTE: Custom deductions (Loan, Uniform, Canteen, Penalty) are ONLY
        # processed via "Process Deductions" button — NOT here in salary mode.
        # This keeps salary and deduction processing separate.
        advance_ded = 0.0
        _loan_ded = 0.0; _canteen_ded = 0.0; _fine_ded = 0.0

        # ── Step 8: Net Salary ─────────────────────────────────
        # Only statutory deductions here (PF, ESIC, PT, LWF, TDS)
        total_deductions = round(pf_emp + esi_emp + pt + lwf + tds, 2)
        skip_deductions = False
        skip_reason = ""
        net_salary = round(gross - total_deductions, 2)

        result = {
            # Attendance
            "working_days":     wd,
            "payable_days":     payable_days,
            "present_days":     present_days,
            "absent_days":      absent_days,
            "paid_leave_days":  paid_leave_days,
            "half_days":        half_days,
            "wop_days":         wop_days,
            "holiday_days":     holiday_days,
            "late_marks":       late_marks,
            "per_day_salary":   round(per_day, 2),
            # Earnings
            "basic_earned":     basic_earned,
            "hra_earned":       hra_earned,
            "special_earned":   special_earned,
            "ot_hours":         ot_hours,
            "ot_amount":        ot_amount,
            "bonus":            bonus_total,
            "gross":            gross,
            # Deductions
            "pf":               pf_emp,
            "employer_pf":      pf_er,
            "esi":              esi_emp,
            "employer_esi":     esi_er,
            "pt":               pt,
            "lwf":              lwf,
            "tds":              tds,
            "advance_deduction":advance_ded,
            "loan_deduction":round(_loan_ded,2),
            "canteen_deduction":round(_canteen_ded,2),
            "fine_deduction":round(_fine_ded,2),
            "total_deductions": total_deductions,
            "net_salary":       net_salary,
            "actual_gross":     actual_gross,
            "skip_deductions":  skip_deductions,
            "skip_reason":      skip_reason,
            # Employee info
            "emp_name":         emp.get("emp_name",""),
            "department":       emp.get("department",""),
            "category":         cat,
            "scheme":           scheme["scheme_name"] if scheme else "Manual",
        }

        # ── Step 9: Save ───────────────────────────────────────
        if not preview:
            try:
                conn.execute("""INSERT OR REPLACE INTO salary_records
                    (emp_code,month,year,category,
                     working_days,payable_days,present_days,absent_days,
                     paid_leave_days,half_days,wop_days,holiday_days,
                     late_marks,per_day_salary,
                     basic_earned,hra_earned,special_earned,
                     ot_hours,ot_amount,bonus,gross,actual_gross,
                     pf,employer_pf,esi,employer_esi,pt,lwf,tds,
                     advance_deduction,loan_deduction,canteen_deduction,fine_deduction,
                     total_deductions,net_salary,skip_deductions,skip_reason,
                     payment_status,generated_on)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (emp_code,month,year,cat,
                     wd,payable_days,present_days,absent_days,
                     paid_leave_days,half_days,wop_days,holiday_days,
                     late_marks,per_day_salary,
                     basic_earned,hra_earned,special_earned,
                     ot_hours,ot_amount,bonus_total,gross,actual_gross,
                     pf_emp,pf_er,esi_emp,esi_er,pt,lwf,tds,
                     round(advance_ded,2),round(_loan_ded,2),round(_canteen_ded,2),round(_fine_ded,2),
                     total_deductions,net_salary,
                     1 if skip_deductions else 0,skip_reason,
                     "Pending",datetime.now().strftime("%Y-%m-%d %H:%M")))
                # Auto-lock after save
                conn.execute("UPDATE salary_records SET locked=1 WHERE emp_code=? AND month=? AND year=?",
                    (emp_code, month, year))
                conn.commit()
            except Exception as insert_err:
                # Fallback: basic INSERT with core columns only
                # Silent fallback - no CMD spam
                conn.execute("""INSERT OR REPLACE INTO salary_records
                    (emp_code,month,year,category,
                     working_days,payable_days,present_days,absent_days,
                     paid_leave_days,half_days,wop_days,holiday_days,
                     late_marks,per_day_salary,
                     basic_earned,hra_earned,special_earned,
                     ot_hours,ot_amount,bonus,gross,
                     pf,employer_pf,esi,employer_esi,pt,lwf,tds,
                     advance_deduction,loan_deduction,canteen_deduction,fine_deduction,
                     total_deductions,net_salary,
                     payment_status,generated_on)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (emp_code,month,year,cat,
                     wd,payable_days,present_days,absent_days,
                     paid_leave_days,half_days,wop_days,holiday_days,
                     late_marks,per_day_salary,
                     basic_earned,hra_earned,special_earned,
                     ot_hours,ot_amount,bonus_total,gross,
                     pf_emp,pf_er,esi_emp,esi_er,pt,lwf,tds,
                     round(advance_ded,2),round(_loan_ded,2),round(_canteen_ded,2),round(_fine_ded,2),
                     total_deductions,net_salary,
                     "Pending",datetime.now().strftime("%Y-%m-%d %H:%M")))
                conn.commit()

            # Audit log
            conn.execute("""INSERT INTO payroll_audit
                (emp_code,month,year,action,new_value,changed_by,changed_on)
                VALUES (?,?,?,'generated',?,?,?)""",
                (emp_code,month,year,f"Net={net_salary}",
                 "System",datetime.now().strftime("%Y-%m-%d %H:%M")))
            conn.commit()

        return result

    except Exception as e:
        import traceback
        err_msg = str(e)
        tb = traceback.format_exc()
        print(f"[PAYROLL ERR] {emp_code}: {err_msg}")
        print(tb)
        # If column missing error, try migrate and retry
        if "no column named" in err_msg or "table salary_records has no column" in err_msg:
            print(f"[PAYROLL] Column missing — run /payroll/migrate first!")
        return None
    finally:
        conn.close()


def get_celebrations():
    """Get today + tomorrow birthdays and anniversaries for ALL active employees."""
    conn = get_db()
    today    = date.today()
    tomorrow = today + timedelta(days=1)
    # Use strftime for reliable MM-DD matching regardless of date format
    td_mmdd = today.strftime("%m-%d")      # e.g. "04-08"
    tm_mmdd = tomorrow.strftime("%m-%d")   # e.g. "04-09"

    # Fetch all active employees with DOB / DOJ
    emps = conn.execute(
        "SELECT emp_name,emp_code,department,date_of_birth,date_of_joining "
        "FROM employees WHERE status='Active'"
    ).fetchall()
    conn.close()

    bt=[]; bto=[]; at=[]; ato=[]
    for e in emps:
        name = e["emp_name"]; code = e["emp_code"]; dept = e["department"] or ""
        # ── Birthday ────────────────────────────────────────────
        dob = (e["date_of_birth"] or "").strip()
        if len(dob) >= 5:
            try:
                # Support YYYY-MM-DD and DD-MM-YYYY and DD/MM/YYYY
                if "-" in dob:
                    parts = dob.split("-")
                else:
                    parts = dob.split("/")
                if len(parts[0]) == 4:          # YYYY-MM-DD
                    mm_dd = f"{parts[1].zfill(2)}-{parts[2][:2].zfill(2)}"
                else:                            # DD-MM-YYYY
                    mm_dd = f"{parts[1].zfill(2)}-{parts[0].zfill(2)}"
                if mm_dd == td_mmdd:
                    bt.append({"name":name,"emp_code":code,"dept":dept})
                elif mm_dd == tm_mmdd:
                    bto.append({"name":name,"emp_code":code,"dept":dept})
            except: pass

        # ── Work Anniversary ────────────────────────────────────
        doj = (e["date_of_joining"] or "").strip()
        if len(doj) >= 5:
            try:
                if "-" in doj:
                    parts = doj.split("-")
                else:
                    parts = doj.split("/")
                if len(parts[0]) == 4:          # YYYY-MM-DD
                    join_year = int(parts[0])
                    mm_dd = f"{parts[1].zfill(2)}-{parts[2][:2].zfill(2)}"
                else:                            # DD-MM-YYYY
                    join_year = int(parts[2])
                    mm_dd = f"{parts[1].zfill(2)}-{parts[0].zfill(2)}"
                if join_year < today.year and mm_dd == td_mmdd:
                    at.append({"name":name,"emp_code":code,"dept":dept,
                               "years": today.year - join_year})
                elif join_year < tomorrow.year and mm_dd == tm_mmdd:
                    ato.append({"name":name,"emp_code":code,"dept":dept,
                                "years": tomorrow.year - join_year})
            except: pass

    return {
        "birthdays_today":        bt,
        "birthdays_tomorrow":     bto,
        "anniversaries_today":    at,
        "anniversaries_tomorrow": ato,
    }

# Permission tree: (code, label, parent_code or None, level)
# ALL modules are now level=1 (independent) — no parent-child dependency
# Each permission works standalone — admin gives exactly what's needed
PERMISSION_TREE = [
    # Dashboard
    ("dashboard",           "Dashboard — View",                 None,  1),
    ("dashboard_kpis",      "Dashboard — KPIs & Charts",        None,  1),
    # Attendance
    ("att_register",        "Attendance — Register & Reports",  None,  1),
    ("manual_entry",        "Attendance — Manual Entry",        None,  1),
    ("machines",            "Attendance — Attendance Machine",  None,  1),
    ("shift_roster",        "Attendance — Shift Roster",        None,  1),
    ("punch_alerts",        "Attendance — Punch Alerts",        None,  1),
    ("holidays",            "Attendance — Holidays",            None,  1),
    ("absent_report",       "Attendance — Absent Report",       None,  1),
    ("late_report",         "Attendance — Late Report",         None,  1),
    # Employees
    ("emp_view",            "Employees — View List",            None,  1),
    ("emp_add_edit",        "Employees — Add / Edit",           None,  1),
    ("emp_export",          "Employees — Export Excel",         None,  1),
    ("masters",             "Employees — Masters (Dept/Shift)", None,  1),
    ("manpower",            "Employees — Manpower Master",      None,  1),
    ("exit_mgmt",           "Employees — Exit Management",      None,  1),
    # Payroll
    ("payroll_view",        "Payroll — View Summary",           None,  1),
    ("payroll_process",     "Payroll — Process Salary",         None,  1),
    ("payroll_mark_paid",   "Payroll — Mark Paid / Lock",       None,  1),
    ("payslip",             "Payroll — Payslips",               None,  1),
    ("payroll_trends",      "Payroll — Trends & Analytics",     None,  1),
    ("ctc_report",         "Payroll — CTC Report",              None,  1),
    ("salary_revision",     "Payroll — Salary Revisions",       None,  1),
    ("working_days",        "Payroll — Working Days Settings",  None,  1),
    ("payroll_schemes",     "Payroll — Schemes",                None,  1),
    ("payroll_settings",    "Payroll — Settings",               None,  1),
    # OT
    ("ot_rates",            "OT — Rate Master",                 None,  1),
    # Leave
    ("leave_approve",       "Leave — Approve / Reject",         None,  1),
    ("leave_balance",       "Leave — Balance View",             None,  1),
    ("leave_master",        "Leave — Master Settings",          None,  1),
    ("leave_assoc",         "OT Payment — Associate OT",        None,  1),
    ("my_leaves",           "Leave — My Leaves (Self)",         None,  1),
    # Reports
    ("reports_att",         "Reports — Attendance Reports",     None,  1),
    ("reports_salary",      "Reports — Salary Reports",         None,  1),
    ("reports_payroll",     "Reports — Payroll Summary",        None,  1),
    # Deductions
    ("deductions_view",     "Deductions — View",                None,  1),
    ("deductions_add",      "Deductions — Add / Edit",          None,  1),
    # Letters
    ("letters_view",        "Letters — Generate Letters",       None,  1),
    ("document_log",        "Letters — Document Log",           None,  1),
    # Reports (additional)
    ("yearly_att",         "Reports — Yearly Attendance",       None,  1),
    # Admin
    ("users",               "Admin — User Management",          None,  1),
    # Self-service (always available to employees)
    ("my_payslip",          "My Payslip (Self)",                None,  1),
]

# Flat list for backward compatibility
PERMISSIONS = [(code, label) for code, label, parent, level in PERMISSION_TREE]

# ─────────────────────────────────────────────────────
#  AUTH
# ─────────────────────────────────────────────────────
def amgr(f):
    from functools import wraps
    @wraps(f)
    def w(*a,**k):
        if "user" not in session: return redirect("/login")
        role = session.get("role","")
        # Only admin gets full unrestricted access
        # hr, manager, director, employee — all go through permission check
        if role == "admin":
            return f(*a,**k)
        perms = set(session.get("permissions",[]) or [])
        path  = request.path

        # Always allow self-service routes regardless of permissions
        self_service = ["/my/change-password", "/my-payslip", "/my-leaves",
                        "/api/celebrations", "/api/emp-search", "/my-"]
        if any(path.startswith(p) for p in self_service):
            return f(*a,**k)

        # PATH → REQUIRED PERMISSION(S)
        # Any perm in the list grants access to VIEW
        # The _edit variant of any perm grants WRITE access
        # IMPORTANT: More specific (longer) paths MUST appear before shorter prefix paths
        # to avoid shorter prefix shadowing longer ones in startswith() match.
        perm_map = [
            # Dashboard — specific sub-routes first
            ("/dashboard/kpi-refresh",  ["dashboard", "dashboard_kpis"]),
            ("/dashboard/dept-emps",    ["dashboard", "dashboard_kpis"]),
            ("/dashboard",              ["dashboard", "dashboard_kpis"]),
            ("/api/attendance-trend",   ["dashboard", "dashboard_kpis", "att_register"]),
            ("/api/payroll-trend",      ["dashboard", "payroll_trends"]),
            ("/api/absence-trend",      ["dashboard", "att_register"]),
            ("/api/dashboard-ot-alert", ["dashboard", "dashboard_kpis", "ot_process"]),
            # Attendance — specific sub-routes first, then general
            ("/attendance/manual-add",  ["manual_entry"]),
            ("/attendance/add-punch",   ["manual_entry"]),
            ("/attendance/punch-log",   ["manual_entry"]),
            ("/attendance/reimport-employee", ["manual_entry"]),
            ("/attendance/machines",    ["machines"]),
            ("/attendance/recalculate", ["att_register"]),
            ("/attendance/fix-weekly-off", ["att_register"]),
            ("/attendance/late-report", ["late_report", "reports_att"]),
            ("/attendance",             ["att_register", "manual_entry"]),
            ("/shift-roster",           ["shift_roster"]),
            ("/shift-groups",           ["shift_roster", "masters"]),
            ("/punch-alerts",           ["punch_alerts"]),
            ("/holidays",               ["holidays"]),
            ("/absent-report",          ["absent_report", "reports_att"]),
            ("/late-report",            ["late_report", "reports_att"]),
            ("/reports/att-data",       ["reports_att", "att_register"]),
            # Employees — specific sub-routes first
            ("/employees/calculate-salary-defaults", ["emp_add_edit"]),
            ("/employees/upload-excel", ["emp_add_edit"]),
            ("/employees/delete",       ["emp_add_edit"]),
            ("/employees/edit",         ["emp_add_edit"]),
            ("/employees/add",          ["emp_add_edit"]),
            ("/employees/get",          ["emp_view", "emp_add_edit"]),
            ("/employees",              ["emp_view", "emp_add_edit"]),
            ("/masters/employee-fields",["masters"]),
            ("/masters",                ["masters"]),
            ("/manpower",               ["manpower"]),
            ("/exit",                   ["exit_mgmt"]),
            ("/shifts",                 ["masters", "shift_roster"]),
            ("/api/emp-info",           ["emp_view", "emp_add_edit"]),
            ("/api/employee-custom-values", ["emp_view", "emp_add_edit"]),
            # Payroll — specific sub-routes first, then general /payroll
            ("/payroll/lock-status",    ["payroll_view", "payroll_mark_paid"]),
            ("/payroll/mark-paid",      ["payroll_mark_paid"]),
            ("/payroll/lock",           ["payroll_mark_paid"]),
            ("/payroll/unlock",         ["payroll_mark_paid"]),
            ("/payroll/bank-file",      ["reports_salary", "payroll_view"]),
            ("/payroll/reports",        ["reports_salary"]),
            ("/payroll/ot-lock",        ["leave_assoc"]),
            ("/payroll/ot-export",      ["ot_rates"]),
            ("/payroll/ot-process",     ["ot_rates"]),
            ("/payroll/ot-rates/save",  ["ot_rates"]),
            ("/payroll/ot-rates",       ["ot_rates"]),
            ("/payroll/leave-associate/export", ["leave_assoc"]),
            ("/payroll/leave-associate/sync",   ["leave_assoc"]),
            ("/payroll/leave-associate/approve",["leave_assoc"]),
            ("/payroll/leave-associate/save",   ["leave_assoc"]),
            ("/payroll/leave-associate",        ["leave_assoc"]),
            ("/payroll/working-days/delete", ["working_days"]),
            ("/payroll/working-days",   ["working_days"]),
            ("/payroll/schemes",        ["payroll_schemes"]),
            ("/payroll/settings",       ["payroll_settings"]),
            ("/payroll/trends",         ["payroll_trends"]),
            ("/payroll/ctc",            ["ctc_report", "payroll_view", "reports_salary"]),
            ("/export/ctc",             ["ctc_report", "payroll_view", "reports_salary"]),
            ("/payroll/summary",        ["payroll_view"]),
            ("/payroll/process",        ["payroll_process"]),
            ("/payroll",                ["payroll_view", "payroll_process"]),
            ("/payslip/get",            ["payslip", "payroll_view"]),
            ("/payslip",                ["payslip", "payroll_view"]),
            ("/salary/increment-letter", ["letters_view", "salary_revision"]),
            ("/salary/revision",        ["salary_revision"]),
            ("/salary",                 ["payroll_view"]),
            # Exports — specific first
            ("/export/attendance-detail-all", ["reports_att", "att_register"]),
            ("/export/attendance-employee",   ["reports_att", "att_register"]),
            ("/export/attendance-range",      ["reports_att", "att_register"]),
            ("/export/attendance",      ["reports_att", "att_register"]),
            ("/export/dept-history",    ["emp_view", "emp_export"]),
            ("/export/employees",       ["emp_view", "emp_export"]),
            ("/export/salary-revision", ["salary_revision", "reports_salary"]),
            ("/export/salary",          ["reports_salary"]),
            ("/export/bank",            ["reports_salary", "payroll_view"]),
            ("/export/ot",              ["ot_process"]),
            ("/export/pf",              ["reports_salary"]),
            ("/export/yearly",          ["reports_salary", "reports_payroll"]),
            ("/export/att",             ["reports_att", "att_register"]),
            ("/export/deductions",      ["deductions_view", "deductions_add"]),
            ("/export/leave-balance",   ["leave_balance"]),
            ("/export/gratuity-bonus",  ["payroll_view", "reports_salary"]),
            # Reports
            ("/reports/data",           ["reports_salary", "reports_payroll"]),
            ("/reports/yearly-attendance", ["reports_att", "yearly_att"]),
            ("/export/yearly-attendance",  ["reports_att", "yearly_att"]),
            ("/reports",                ["reports_att", "reports_salary", "reports_payroll"]),
            # Leave — specific first
            ("/leaves/manage",          ["leave_approve"]),
            ("/leaves/approved",        ["leave_approve", "leave_balance"]),
            ("/leaves/balance",         ["leave_balance"]),
            ("/leave-master",           ["leave_master"]),
            ("/leaves",                 ["leave_approve", "leave_balance", "my_leaves"]),
            # Deductions
            ("/deductions",             ["deductions_view", "deductions_add"]),
            # Letters — specific first
            ("/letters/settings/remove",    ["users"]),
            ("/letters/project-experience", ["letters_view"]),
            ("/letters/resignation-print",  ["letters_view"]),
            ("/letters",                ["letters_view"]),
            ("/documents",              ["document_log"]),
            # Admin
            ("/users",                  ["users"]),
            # Misc admin routes (always allow for admins, covered by role check above)
            ("/calendar",               ["dashboard", "att_register"]),
            ("/gratuity-bonus",         ["payroll_view", "payroll_process"]),
            ("/admin/",                 ["users"]),
            ("/email-settings",         ["users"]),
            ("/email/",                 ["users"]),
        ]

        # Find matching permission group for this path — longer/specific paths listed first
        needed_perms = None
        for prefix, plist in perm_map:
            if path.startswith(prefix):
                needed_perms = plist; break

        # No perm rule found → deny
        if needed_perms is None:
            if path.startswith("/api/") or path.startswith("/export/"):
                return jsonify({"success":False,"error":"Permission denied"}), 403
            return redirect("/my-payslip")

        # Check if user has ANY of the needed permissions (view access)
        has_view = any(p in perms for p in needed_perms)
        # Check edit: user has _edit variant OR the perm itself implies write access
        _edit_keywords = ("_add_edit", "_add", "_process", "_mark_paid", "_approve",
                          "_edit", "_update", "_delete", "_upload", "_bulk")
        has_edit = has_view and any(
            (p+"_edit") in perms or any(p.endswith(kw) for kw in _edit_keywords)
            for p in needed_perms if p in perms
        )
        # For GET requests: view permission is enough
        # For POST/PUT/DELETE: need edit permission too
        if request.method in ("POST","PUT","DELETE","PATCH"):
            if not has_edit and not has_view:
                return jsonify({"success":False,"error":"Permission denied"}), 403
            # View-only users cannot POST
            if has_view and not has_edit:
                # Allow safe POST routes (like loading data)
                _safe_post_paths = ["/payroll/preview", "/api/", "/att-detail", "/get-balance", "/get-record"]
                if not any(path.startswith(sp) for sp in _safe_post_paths):
                    return jsonify({"success":False,"error":"View-only access — edit permission required"}), 403

        if not has_view and not has_edit:
            if path.startswith("/api/") or path.startswith("/export/") or path.startswith("/reports/data"):
                return jsonify({"success":False,"error":"Access denied — insufficient permissions"}), 403
            # Friendly error page — no redirect to dashboard (they may not have access)
            from flask import render_template_string
            _back = "/my-payslip" if session.get("role")=="employee" else "/my-payslip"
            return render_template_string("""<!DOCTYPE html>
<html><head><title>Access Restricted</title>
<style>body{font-family:Arial,sans-serif;background:#0a0f1e;color:#e2e8f0;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;}
.box{text-align:center;padding:40px;background:#1a2035;border-radius:16px;border:1px solid #2d3a52;max-width:400px;}
.icon{font-size:48px;margin-bottom:16px;}h2{margin:0 0 10px;}p{color:#94a3b8;margin:10px 0 20px;}
a{background:#0052cc;color:#fff;padding:10px 24px;border-radius:8px;text-decoration:none;font-weight:700;}</style>
</head><body><div class="box">
<div class="icon">🔒</div>
<h2>Access Restricted</h2>
<p>You do not have permission to access this page.<br>Contact your administrator to request access.</p>
<a href="{{ back }}">← Go Back</a>
</div></body></html>""", back=_back), 403

        return f(*a,**k)
    return w

def lreq(f):
    from functools import wraps
    @wraps(f)
    def w(*a,**k):
        if "user" not in session: return redirect("/login")
        return f(*a,**k)
    return w



# ─── MANPOWER MASTER ────────────────────────────────
@app.route("/manpower")
@amgr
def manpower_page():
    conn = get_db()
    # Show ALL departments from dept_manpower (synced from Masters)
    # Plus any departments that have employees but aren't in dept_manpower yet
    manpower_rows = conn.execute("SELECT * FROM dept_manpower ORDER BY department").fetchall()
    manpower = {r["department"]: dict(r) for r in manpower_rows}
    # Also get departments from employees not yet in manpower
    emp_depts = conn.execute("SELECT DISTINCT department FROM employees WHERE status='Active' AND department IS NOT NULL ORDER BY department").fetchall()
    for ed in emp_depts:
        d = ed["department"]
        if d and d not in manpower:
            conn.execute("INSERT OR IGNORE INTO dept_manpower (department,std_staff,std_nonstaff) VALUES (?,0,0)", (d,))
            manpower[d] = {"department":d,"std_staff":0,"std_nonstaff":0}
    conn.commit()
    # Use dept_manpower as source of truth for departments list
    depts = [{"department": k} for k in sorted(manpower.keys())]
    actual = conn.execute("""SELECT department,
        SUM(CASE WHEN category='Staff' THEN 1 ELSE 0 END) as staff_count,
        SUM(CASE WHEN category='Associate' THEN 1 ELSE 0 END) as assoc_count
        FROM employees WHERE status='Active' GROUP BY department""").fetchall()
    conn.close()
    return render_template("manpower.html", depts=[d["department"] for d in depts],
        manpower=manpower, actual=[dict(a) for a in actual])

@app.route("/manpower/save", methods=["POST"])
@amgr
def manpower_save():
    d = request.json; conn = get_db()
    try:
        for dept, vals in d.items():
            conn.execute("""INSERT INTO dept_manpower (department,std_staff,std_nonstaff,updated_by,updated_on)
                VALUES (?,?,?,?,datetime('now'))
                ON CONFLICT(department) DO UPDATE SET
                std_staff=excluded.std_staff, std_nonstaff=excluded.std_nonstaff,
                updated_by=excluded.updated_by, updated_on=excluded.updated_on""",
                (dept, int(vals.get("staff",0)), int(vals.get("nonstaff",0)), session.get("name","HR")))
        conn.commit(); return jsonify({"success":True})
    except Exception as e: return jsonify({"success":False,"error":str(e)})
    finally: conn.close()

# ─── DASHBOARD DRILLDOWN API ────────────────────────
@app.route("/dashboard/dept-detail")
@amgr
def dashboard_dept_detail():
    box = request.args.get("box","active")  # active/present/absent/ot
    conn = get_db()
    today_str = date.today().strftime("%Y-%m-%d")
    m = date.today().month; y = date.today().year
    month_str = f"{m:02d}"

    if box == "active":
        rows = conn.execute("""SELECT department,
            SUM(CASE WHEN category='Staff' THEN 1 ELSE 0 END) as staff_count,
            SUM(CASE WHEN category='Associate' THEN 1 ELSE 0 END) as assoc_count
            FROM employees WHERE status='Active' GROUP BY department ORDER BY department""").fetchall()
        manpower = {r["department"]: dict(r) for r in conn.execute("SELECT * FROM dept_manpower").fetchall()}
        data = []
        for r in rows:
            mp = manpower.get(r["department"],{})
            data.append({"dept": r["department"] or "N/A",
                "staff": r["staff_count"], "nonstaff": r["assoc_count"],
                "std_staff": mp.get("std_staff",0), "std_nonstaff": mp.get("std_nonstaff",0)})

    elif box == "present":
        rows = conn.execute("""SELECT e.department,
            SUM(CASE WHEN e.category='Staff' THEN 1 ELSE 0 END) as staff_count,
            SUM(CASE WHEN e.category='Associate' THEN 1 ELSE 0 END) as assoc_count
            FROM attendance a JOIN employees e ON a.emp_code=e.emp_code
            WHERE a.att_date=? AND a.status NOT IN ('Absent','WO') AND e.status='Active'
            GROUP BY e.department ORDER BY e.department""", [today_str]+d_params).fetchall()
        data = [{"dept": r["department"] or "N/A","staff": r["staff_count"],"nonstaff": r["assoc_count"]} for r in rows]

    elif box == "absent":
        # Active - Present - Yet-to-arrive - WO = Absent per dept
        # Use same logic as main KPI: all shifts + 30 min grace + WO exclusion
        from datetime import datetime as _dtnow_dd
        _now_min_dd = _dtnow_dd.now().hour * 60 + _dtnow_dd.now().minute
        _GRACE_DD = 30
        _wo_day_names_dd = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
        _today_wo_dd = _wo_day_names_dd[__import__('datetime').date.today().weekday()]

        # Employees whose shift hasn't started yet (all shifts + grace)
        yet_arrive_dd = set()
        all_shifts_dd = conn.execute("""
            SELECT srd.emp_code, s.start_time
            FROM shift_roster_dates srd
            JOIN shifts s ON srd.shift_id=s.id
            JOIN employees e ON srd.emp_code=e.emp_code
            WHERE srd.shift_date=? AND e.status='Active'
        """, (today_str,)).fetchall()
        for ns in all_shifts_dd:
            try:
                sh_h, sh_m = map(int, (ns["start_time"] or "09:00").split(":")[:2])
                if _now_min_dd < sh_h*60 + sh_m + _GRACE_DD:
                    ha = conn.execute(
                        "SELECT status FROM attendance WHERE emp_code=? AND att_date=?",
                        (ns["emp_code"], today_str)).fetchone()
                    if not ha or ha["status"] in ("Absent","WO"):
                        # Also skip if today is their WO
                        wo_r = conn.execute(
                            "SELECT COALESCE(weekly_off,'Sunday') as wo FROM employees WHERE emp_code=?",
                            (ns["emp_code"],)).fetchone()
                        if wo_r and wo_r["wo"] == _today_wo_dd:
                            pass  # WO - don't add to yet_arrive
                        else:
                            yet_arrive_dd.add(ns["emp_code"])
            except: pass

        active = {r["department"]: {"staff": r["staff_count"],"nonstaff": r["assoc_count"]}
            for r in conn.execute("""SELECT department,
                SUM(CASE WHEN category='Staff' THEN 1 ELSE 0 END) as staff_count,
                SUM(CASE WHEN category='Associate' THEN 1 ELSE 0 END) as assoc_count
                FROM employees WHERE status='Active' GROUP BY department""").fetchall()}
        present = {r["department"]: {"staff": r["staff_count"],"nonstaff": r["assoc_count"]}
            for r in conn.execute("""SELECT e.department,
                SUM(CASE WHEN e.category='Staff' THEN 1 ELSE 0 END) as staff_count,
                SUM(CASE WHEN e.category='Associate' THEN 1 ELSE 0 END) as assoc_count
                FROM attendance a JOIN employees e ON a.emp_code=e.emp_code
                WHERE a.att_date=? AND a.status NOT IN ('Absent','WO') AND e.status='Active'
                GROUP BY e.department""", (today_str,)).fetchall()}
        # WO employees per dept who did NOT come today (WOP = came on WO day → already in present)
        wo_by_dept = {}
        wo_emps_dd = conn.execute("""
            SELECT e.department, e.category, e.emp_code
            FROM employees e
            WHERE e.status='Active' AND COALESCE(e.weekly_off,'Sunday')=?""",
            (_today_wo_dd,)).fetchall()
        # Get employees who came today (any status except Absent/WO)
        came_today_dd = {r["emp_code"] for r in conn.execute(
            "SELECT emp_code FROM attendance WHERE att_date=? AND status NOT IN ('Absent','WO')",
            (today_str,)).fetchall()}
        for we in wo_emps_dd:
            # Only count as "WO-absent" if they did NOT come today
            if we["emp_code"] in came_today_dd:
                continue  # They came on WO day (WOP) → already in present count
            dk = we["department"]
            if dk not in wo_by_dept: wo_by_dept[dk] = {"staff":0,"nonstaff":0}
            if we["category"]=="Staff": wo_by_dept[dk]["staff"] += 1
            else: wo_by_dept[dk]["nonstaff"] += 1

        # Yet-to-arrive per dept
        yat_by_dept = {}
        if yet_arrive_dd:
            for ec in yet_arrive_dd:
                er = conn.execute(
                    "SELECT department, category FROM employees WHERE emp_code=?", (ec,)).fetchone()
                if er:
                    dk = er["department"]
                    if dk not in yat_by_dept: yat_by_dept[dk] = {"staff":0,"nonstaff":0}
                    if er["category"]=="Staff": yat_by_dept[dk]["staff"] += 1
                    else: yat_by_dept[dk]["nonstaff"] += 1

        data = []
        for dept, ac in active.items():
            pr  = present.get(dept, {"staff":0,"nonstaff":0})
            wo  = wo_by_dept.get(dept, {"staff":0,"nonstaff":0})
            yat = yat_by_dept.get(dept, {"staff":0,"nonstaff":0})
            abs_s = max(0, ac["staff"]    - pr["staff"]    - wo["staff"]    - yat["staff"])
            abs_a = max(0, ac["nonstaff"] - pr["nonstaff"] - wo["nonstaff"] - yat["nonstaff"])
            if abs_s + abs_a > 0:
                data.append({"dept": dept or "N/A","staff": abs_s,"nonstaff": abs_a})
        data.sort(key=lambda x: x["dept"])

    elif box == "ot":
        rows = conn.execute("""SELECT e.department,
            SUM(CASE WHEN e.category='Staff' THEN COALESCE(a.ot_minutes,0) ELSE 0 END) as staff_ot,
            SUM(CASE WHEN e.category='Associate' THEN COALESCE(a.ot_minutes,0) ELSE 0 END) as assoc_ot
            FROM attendance a JOIN employees e ON a.emp_code=e.emp_code
            WHERE strftime('%m',a.att_date)=? AND strftime('%Y',a.att_date)=?
            AND a.att_date < ? AND e.status='Active'
            GROUP BY e.department ORDER BY e.department""", (month_str,str(y),today_str)).fetchall()
        data = [{"dept": r["department"] or "N/A",
            "staff_ot": round(r["staff_ot"]/60,1),
            "nonstaff_ot": round(r["assoc_ot"]/60,1)} for r in rows]
    else:
        data = []
    conn.close()
    return jsonify({"success":True,"box":box,"data":data})


# ─────────────────────────────────────────────────────
#  ROUTES
# ─────────────────────────────────────────────────────
@app.route("/")
def index():
    if "user" not in session: return redirect("/login")
    return redirect("/my-payslip" if session.get("role")=="employee" else "/dashboard")


@app.route("/dashboard/kpi-refresh")
@amgr
def dashboard_kpi_refresh():
    """Lightweight endpoint to refresh KPI numbers every minute"""
    conn = get_db()
    today_str = date.today().strftime("%Y-%m-%d")
    m = date.today().month; y = date.today().year
    month_str = f"{m:02d}"
    # Dept filter for non-admin users
    dept_sql, dept_params = get_user_dept_filter("e")
    try:
        total   = conn.execute(f"SELECT COUNT(*) FROM employees e WHERE status='Active'{dept_sql}",
                               dept_params).fetchone()[0]
        present = conn.execute(f"""SELECT COUNT(DISTINCT a.emp_code) FROM attendance a
            JOIN employees e ON a.emp_code=e.emp_code
            WHERE a.att_date=? AND a.status NOT IN ('Absent','WO') AND e.status='Active'{dept_sql}""",
            [today_str]+dept_params).fetchone()[0]
        # Yet-to-Arrive Guard for KPI refresh (all shifts + 30 min grace)
        from datetime import datetime as _dtnow_kpi
        _now_min_kpi = _dtnow_kpi.now().hour * 60 + _dtnow_kpi.now().minute
        _GRACE_KPI = 30
        all_shifts_kpi = conn.execute("""
            SELECT srd.emp_code, s.start_time FROM shift_roster_dates srd
            JOIN shifts s ON srd.shift_id=s.id
            JOIN employees e ON srd.emp_code=e.emp_code
            WHERE srd.shift_date=? AND e.status='Active'
        """, (today_str,)).fetchall()
        yet_arrive_excl = 0
        for ns in all_shifts_kpi:
            try:
                sh_h, sh_m = map(int, (ns["start_time"] or "09:00").split(":")[:2])
                if _now_min_kpi < sh_h * 60 + sh_m + _GRACE_KPI:
                    ha = conn.execute("SELECT status FROM attendance WHERE emp_code=? AND att_date=?",
                        (ns["emp_code"], today_str)).fetchone()
                    if not ha or ha["status"] in ("Absent","WO"):
                        yet_arrive_excl += 1
            except: pass
        absent  = max(0, total - present - yet_arrive_excl)
        # Subtract WO employees
        _wo_names_list = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
        _today_wo_kpi = _wo_names_list[date.today().weekday()]
        wo_today_kpi = conn.execute(
            f"SELECT COUNT(*) FROM employees e WHERE e.status='Active' AND COALESCE(e.weekly_off,'Sunday')=?{dept_sql}",
            [_today_wo_kpi]+dept_params).fetchone()[0]
        absent = max(0, absent - wo_today_kpi)
        ot_min  = conn.execute(f"""SELECT COALESCE(SUM(a.ot_minutes),0) FROM attendance a
            JOIN employees e ON a.emp_code=e.emp_code
            WHERE e.category='Associate' AND e.status='Active'
            AND strftime('%m',a.att_date)=? AND strftime('%Y',a.att_date)=?
            AND a.att_date < ?{dept_sql}""",
            [month_str, str(y), today_str]+dept_params).fetchone()[0]
        conn.close()
        return jsonify({"success":True,"total":total,"present":present,
                        "absent":absent,"ot":round(ot_min/60,1)})
    except Exception as e:
        conn.close()
        return jsonify({"success":False,"error":str(e)})

@app.route("/dashboard/dept-emps")
@amgr
def dashboard_dept_emps():
    dept = request.args.get("dept","")
    box  = request.args.get("box","active")  # active/present/absent/ot
    conn = get_db()
    today_str = date.today().strftime("%Y-%m-%d")

    if box == "active":
        emps = conn.execute("""SELECT emp_code,emp_name,designation,category,status,
            COALESCE(phone,'') as phone
            FROM employees WHERE status='Active' AND department=?
            ORDER BY category,emp_name""", (dept,)).fetchall()
        result = [dict(e) for e in emps]

    elif box == "present":
        rows = conn.execute("""SELECT e.emp_code,e.emp_name,e.designation,e.category,
            a.in_time,a.out_time,a.status as att_status
            FROM employees e JOIN attendance a ON e.emp_code=a.emp_code
            WHERE e.department=? AND a.att_date=? AND a.status NOT IN ('Absent','WO')
            AND e.status='Active' ORDER BY e.category,e.emp_name""",
            (dept, today_str)).fetchall()
        result = [dict(r) for r in rows]

    elif box == "absent":
        # Absent = Active in dept - Present - WO today - Yet-to-arrive (same logic as KPI)
        from datetime import datetime as _dtnow_de
        _now_min_de = _dtnow_de.now().hour * 60 + _dtnow_de.now().minute
        _GRACE_DE = 30
        _wo_names_de = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
        _today_wo_de = _wo_names_de[date.today().weekday()]

        # All active employees in this dept
        all_emps = {r["emp_code"]: dict(r) for r in conn.execute(
            """SELECT emp_code, emp_name, designation, category,
               COALESCE(phone,'') as phone, COALESCE(weekly_off,'Sunday') as weekly_off
               FROM employees WHERE status='Active' AND department=?
               ORDER BY category, emp_name""", (dept,)).fetchall()}

        # Present today in this dept
        present_today = {r["emp_code"] for r in conn.execute(
            """SELECT e.emp_code FROM attendance a JOIN employees e
               ON a.emp_code=e.emp_code
               WHERE e.department=? AND a.att_date=?
               AND a.status NOT IN ('Absent','WO')""",
            (dept, today_str)).fetchall()}

        # Yet-to-arrive in this dept (shift not started + 30 min grace)
        yet_arrive_de = set()
        ns_today_de = conn.execute("""
            SELECT srd.emp_code, s.start_time
            FROM shift_roster_dates srd
            JOIN shifts s ON srd.shift_id=s.id
            JOIN employees e ON srd.emp_code=e.emp_code
            WHERE srd.shift_date=? AND e.status='Active' AND e.department=?
        """, (today_str, dept)).fetchall()
        for ns in ns_today_de:
            try:
                sh_h, sh_m = map(int, (ns["start_time"] or "09:00").split(":")[:2])
                if _now_min_de < sh_h*60 + sh_m + _GRACE_DE:
                    ha = conn.execute(
                        "SELECT status FROM attendance WHERE emp_code=? AND att_date=?",
                        (ns["emp_code"], today_str)).fetchone()
                    if not ha or ha["status"] in ("Absent","WO"):
                        # Only add if not WO today
                        emp_info = all_emps.get(ns["emp_code"])
                        if emp_info and emp_info["weekly_off"] != _today_wo_de:
                            yet_arrive_de.add(ns["emp_code"])
            except: pass

        result = [v for k, v in all_emps.items()
                  if k not in present_today
                  and k not in yet_arrive_de
                  and v["weekly_off"] != _today_wo_de]

    elif box == "ot":
        m = date.today().month; y = date.today().year
        rows = conn.execute("""SELECT e.emp_code,e.emp_name,e.designation,e.category,
            SUM(a.ot_minutes) as total_ot
            FROM employees e JOIN attendance a ON e.emp_code=a.emp_code
            WHERE e.department=? AND e.category='Associate'
            AND strftime('%m',a.att_date)=? AND strftime('%Y',a.att_date)=?
            AND e.status='Active' AND a.ot_minutes>0
            GROUP BY e.emp_code ORDER BY total_ot DESC""",
            (dept, f"{m:02d}", str(y))).fetchall()
        result = [dict(r) for r in rows]
    else:
        result = []

    conn.close()
    return jsonify({"success":True,"employees":result,"box":box})

@app.route("/manpower/delete/<dept>", methods=["POST"])
@amgr
def manpower_delete_dept(dept):
    from urllib.parse import unquote
    dept = unquote(dept)
    conn = get_db()
    conn.execute("DELETE FROM dept_manpower WHERE department=?", (dept,))
    conn.execute("DELETE FROM dept_ot_limits WHERE department=?", (dept,))
    conn.commit(); conn.close()
    return jsonify({"success": True})


@app.route("/api/emp-info")
@lreq
def api_emp_info():
    emp_code = request.args.get("emp_code","").strip()
    conn = get_db()
    emp = conn.execute("SELECT emp_code,emp_name,department,category,basic,hra,special_allowance FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
    conn.close()
    if not emp: return jsonify({"success":False,"error":"Not found"})
    return jsonify({"success":True,"emp_code":emp["emp_code"],"emp_name":emp["emp_name"],
                   "department":emp["department"],"category":emp["category"],
                   "basic":emp["basic"],"hra":emp["hra"],"special_allowance":emp["special_allowance"]})


def log_act(action, details=""):
    """Helper to log user action"""
    if "user" not in session: return
    try:
        conn = get_db()
        conn.execute("""INSERT INTO activity_log (username,action,page,details,ip_address)
            VALUES (?,?,?,?,?)""",
            (session.get("user",""), action, request.path, details[:500], request.remote_addr))
        conn.commit(); conn.close()
    except: pass

@app.route("/login",methods=["GET","POST"])
def login():
    err=""
    if request.method=="POST":
        u=request.form.get("username","").strip()
        p=request.form.get("password","").strip()
        conn=get_db()
        user=conn.execute("SELECT * FROM users WHERE username=? AND is_active=1",(u,)).fetchone()
        conn.close()
        if user and user["password"]==hp(p):
            # Load user permissions into session
            conn2 = get_db()
            try:
                perm_rows = conn2.execute("SELECT permission FROM user_permissions WHERE user_id=?", (user["id"],)).fetchall()
                perms = [r["permission"] for r in perm_rows]
            except:
                perms = []
            conn2.close()
            # Only admin gets all permissions auto-assigned
            # All other roles (hr, manager, director, employee) use only their assigned permissions
            if user["role"] == "admin":
                perms = [perm[0] for perm in PERMISSIONS]
            else:
                # Strict individual permissions — exactly what admin has assigned
                perm_set = set(perms)
                perms = list(perm_set)
            # Ensure employees have at least payslip + leaves
            if not perms:
                perms = ["my_payslip","my_leaves"]
            # Load dept access
            conn_d = get_db()
            try:
                dept_rows = conn_d.execute("SELECT department FROM user_dept_access WHERE user_id=?", (user["id"],)).fetchall()
                dept_access = [r["department"] for r in dept_rows]
            except:
                dept_access = []
            conn_d.close()
            # Admin/hr/director get all departments
            if user["role"] in ("admin","hr","director","manager"):
                dept_access = []  # empty = all departments
            session.update({"user":user["username"],"role":user["role"],"name":user["name"],
                           "emp_id":user["emp_id"],"permissions":perms,
                           "dept_access":dept_access})
            # Log login activity
            try:
                conn3=get_db()
                conn3.execute("INSERT INTO activity_log (user_id,username,action,page,ip_address) VALUES (?,?,?,?,?)",
                    (user["id"],user["username"],"Login","/login",request.remote_addr))
                conn3.commit(); conn3.close()
            except: pass
            # Redirect based on role and permissions
            _perms = set(perms)
            if user["role"] == "employee" or (not _perms & {"dashboard","dashboard_kpis","payroll_view","att_register","emp_view"}):
                return redirect("/my-payslip")
            return redirect("/dashboard")
        err="Invalid username or password!"
    return render_template("login.html",error=err)

@app.route("/logout")
def logout(): session.clear(); return redirect("/login")

@app.route("/my/change-password", methods=["POST"])
def change_my_password():
    """Any logged-in user can change their own password"""
    if "user" not in session:
        return jsonify({"success": False, "error": "Not logged in"})
    d = request.json or {}
    old_pw = d.get("old_password","")
    new_pw = d.get("new_password","")
    if not old_pw or not new_pw:
        return jsonify({"success": False, "error": "Both fields required"})
    if len(new_pw) < 4:
        return jsonify({"success": False, "error": "Password too short (min 4 chars)"})
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE username=?",
        (session["user"],)).fetchone()
    if not user or user["password"] != hp(old_pw):
        conn.close()
        return jsonify({"success": False, "error": "Current password is incorrect"})
    conn.execute("UPDATE users SET password=? WHERE username=?",
        (hp(new_pw), session["user"]))
    conn.commit(); conn.close()
    return jsonify({"success": True, "message": "Password changed successfully!"})


@app.route("/api/today-celebrations")
@lreq
def today_celebrations():
    """Today's birthdays and work anniversaries + tomorrow's (1 day advance)"""
    from datetime import date as _dt, timedelta as _td
    today = _dt.today()
    tomorrow = today + _td(days=1)
    conn = get_db()
    emps = conn.execute("SELECT * FROM employees WHERE status='Active'").fetchall()
    bdays_today=[]; bdays_tom=[]; anni_today=[]; anni_tom=[]
    for e in emps:
        # Birthday
        if e["date_of_birth"]:
            try:
                dob = _dt.fromisoformat(e["date_of_birth"])
                if dob.month==today.month and dob.day==today.day:
                    bdays_today.append({"name":e["emp_name"],"code":e["emp_code"],"dept":e["department"],"date":e["date_of_birth"]})
                elif dob.month==tomorrow.month and dob.day==tomorrow.day:
                    bdays_tom.append({"name":e["emp_name"],"code":e["emp_code"],"dept":e["department"],"date":e["date_of_birth"]})
            except: pass
        # Work Anniversary
        if e["date_of_joining"]:
            try:
                doj = _dt.fromisoformat(e["date_of_joining"])
                yrs = today.year - doj.year
                if yrs>0 and doj.month==today.month and doj.day==today.day:
                    anni_today.append({"name":e["emp_name"],"code":e["emp_code"],"dept":e["department"],"years":yrs,"date":e["date_of_joining"]})
                elif yrs>0 and doj.month==tomorrow.month and doj.day==tomorrow.day:
                    anni_tom.append({"name":e["emp_name"],"code":e["emp_code"],"dept":e["department"],"years":today.year-doj.year,"date":e["date_of_joining"]})
            except: pass
    conn.close()
    return jsonify({"success":True,
        "birthdays_today":bdays_today,"birthdays_tomorrow":bdays_tom,
        "anniversaries_today":anni_today,"anniversaries_tomorrow":anni_tom,
        "today":today.strftime("%d %B %Y"),"tomorrow":tomorrow.strftime("%d %B %Y")})



@app.route("/api/att-detail/<emp_code>/<int:month>/<int:year>")
@lreq
def att_detail_api(emp_code, month, year):
    """Day-wise attendance for payroll summary popup."""
    conn = get_db()
    try:
        rows = conn.execute("""SELECT att_date,status,in_time,out_time,
            working_minutes,late_minutes,late_waived,ot_minutes,is_half_day
            FROM attendance WHERE emp_code=?
            AND strftime('%m',att_date)=? AND strftime('%Y',att_date)=?
            ORDER BY att_date""", (emp_code,f"{month:02d}",str(year))).fetchall()
        emp = conn.execute("SELECT weekly_off FROM employees WHERE emp_code=?",
                           (emp_code,)).fetchone()
        conn.close()
        _days=["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
        records=[]; present=absent=wo=leave=wop=half=late_c=ot_tot=0
        for r in rows:
            dt=date.fromisoformat(r["att_date"])
            wm=r["working_minutes"] or 0; ot=r["ot_minutes"] or 0
            lm=r["late_minutes"] or 0; lw=r["late_waived"] or 0
            st=r["status"] or "—"
            # If waived, show as 0 late
            display_late = 0 if lw else lm
            records.append({"date":dt.strftime("%d %b"),"day_name":_days[dt.weekday()],
                "status":st,"in_time":r["in_time"] or "","out_time":r["out_time"] or "",
                "duration":f"{wm//60}:{wm%60:02d}" if wm>0 else "—",
                "late_min":display_late,"late_waived":lw,"ot_hrs":round(ot/60,2) if ot else 0})
            if st in ("Present","WOP","HP"): present+=1
            elif st=="Absent": absent+=1
            elif st=="WO": wo+=1
            elif st=="Leave": leave+=1
            if r["is_half_day"]: half+=1
            if lm>0 and not lw: late_c+=1  # Only count non-waived late
            ot_tot+=ot
        from calendar import monthrange as _mr
        _wo_map2={"Monday":0,"Tuesday":1,"Wednesday":2,"Thursday":3,"Friday":4,"Saturday":5,"Sunday":6}
        wo_day=_wo_map2.get((emp["weekly_off"] if emp and emp["weekly_off"] else "Sunday"),6)
        _,dim=_mr(year,month)
        wd=sum(1 for d in range(1,dim+1) if date(year,month,d).weekday()!=wo_day)
        return jsonify({"success":True,"records":records,
            "summary":{"working_days":wd,"present":present,"absent":absent,
                "wo":wo,"leave":leave,"wop":wop,"half":half,
                "late":late_c,"ot_hrs":round(ot_tot/60,1)}})
    except Exception as e:
        try: conn.close()
        except: pass
        return jsonify({"success":False,"error":str(e)})


@app.route("/api/attendance-trend")
@amgr
def attendance_trend_api():
    """Attendance trend — absent + present counts by day, up to yesterday"""
    from datetime import date as _dt, timedelta as _td
    period   = request.args.get("period","30d")  # 1d,7d,30d,custom,monthly
    dept     = request.args.get("dept","")
    cat      = request.args.get("cat","")
    gender   = request.args.get("gender","")
    emp_code = request.args.get("emp","")
    from_str = request.args.get("from","")
    to_str   = request.args.get("to","")

    yesterday = _dt.today() - _td(days=1)

    # Build date range
    if period == "1d":
        from_date = yesterday; to_date = yesterday
    elif period == "7d":
        from_date = yesterday - _td(days=6); to_date = yesterday
    elif period == "30d":
        from_date = yesterday - _td(days=29); to_date = yesterday
    elif period == "monthly":
        from_date = _dt(yesterday.year, yesterday.month, 1); to_date = yesterday
    elif from_str and to_str:
        try:
            from_date = _dt.fromisoformat(from_str)
            to_date   = min(_dt.fromisoformat(to_str), yesterday)
        except:
            from_date = yesterday - _td(days=29); to_date = yesterday
    else:
        from_date = yesterday - _td(days=29); to_date = yesterday

    conn = get_db()
    # Build filters
    extra = ""
    params_base = []
    if dept:     extra += " AND e.department=?";  params_base.append(dept)
    elif True:
        # Apply user's dept restriction if no specific dept requested
        d_sql, d_params = get_user_dept_filter("e")
        extra += d_sql; params_base.extend(d_params)
    if cat:      extra += " AND e.category=?";    params_base.append(cat)
    if gender:   extra += " AND e.gender=?";      params_base.append(gender)
    if emp_code: extra += " AND a.emp_code=?";    params_base.append(emp_code)

    fd = from_date.strftime("%Y-%m-%d")
    td = to_date.strftime("%Y-%m-%d")

    # Count Present: status NOT Absent/WO/Holiday
    present_rows = conn.execute(f"""
        SELECT a.att_date, COUNT(*) as cnt
        FROM attendance a JOIN employees e ON a.emp_code=e.emp_code
        WHERE a.att_date BETWEEN ? AND ?
        AND a.status IN ('Present','WOP','HP','Leave')
        AND e.status='Active'{extra}
        GROUP BY a.att_date ORDER BY a.att_date
    """, [fd, td]+params_base).fetchall()

    # Count Absent: status='Absent' in attendance table
    absent_rows = conn.execute(f"""
        SELECT a.att_date, COUNT(*) as cnt
        FROM attendance a JOIN employees e ON a.emp_code=e.emp_code
        WHERE a.att_date BETWEEN ? AND ?
        AND a.status='Absent'
        AND e.status='Active'{extra}
        GROUP BY a.att_date ORDER BY a.att_date
    """, [fd, td]+params_base).fetchall()

    # Depts + categories for filters
    depts = [d["department"] for d in conn.execute(
        "SELECT DISTINCT department FROM employees WHERE status='Active' AND department IS NOT NULL ORDER BY department"
    ).fetchall()]
    conn.close()

    # Build date-indexed maps
    absent_map  = {r["att_date"]: r["cnt"] for r in absent_rows}
    present_map = {r["att_date"]: r["cnt"] for r in present_rows}

    labels=[]; absent_vals=[]; present_vals=[]
    cur = from_date
    while cur <= to_date:
        ds = cur.strftime("%Y-%m-%d")
        labels.append(ds)
        absent_vals.append(absent_map.get(ds,0))
        present_vals.append(present_map.get(ds,0))
        cur += _td(days=1)

    return jsonify({"success":True,
        "labels":labels,
        "absent":absent_vals,
        "present":present_vals,
        "from_date":fd, "to_date":td,
        "departments":depts,
        "period":period})


@app.route("/api/payroll-trend")
@amgr
def payroll_trend_api():
    """Payroll trend — monthly payout, present days, etc."""
    dept   = request.args.get("dept","")
    cat    = request.args.get("cat","")
    gender = request.args.get("gender","")
    metric = request.args.get("metric","net_salary")  # net_salary, gross, pf, esi, present_days, ot_hours
    months_back = int(request.args.get("months","12"))

    from datetime import date as _dt
    today = _dt.today()
    conn = get_db()

    extra = ""
    params = []
    if dept:   extra += " AND e.department=?"; params.append(dept)
    elif True:
        d_sql, d_params = get_user_dept_filter("e")
        extra += d_sql; params.extend(d_params)
    if cat:    extra += " AND e.category=?";   params.append(cat)
    if gender: extra += " AND e.gender=?";     params.append(gender)

    mnames = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

    # OT metrics: pull from attendance table
    if metric in ("ot_hours", "ot_amount"):
        from datetime import datetime as _dtm
        ot_rows = conn.execute(f"""
            SELECT strftime('%m', a.att_date) as mn,
                   strftime('%Y', a.att_date) as yr,
                   SUM(a.ot_minutes) as total_ot,
                   COUNT(DISTINCT a.emp_code) as emp_count
            FROM attendance a JOIN employees e ON a.emp_code=e.emp_code
            WHERE a.ot_minutes > 0{extra}
            GROUP BY yr, mn
            ORDER BY yr DESC, mn DESC
            LIMIT ?
        """, params+[months_back]).fetchall()
        depts = [d["department"] for d in conn.execute(
            "SELECT DISTINCT department FROM employees WHERE status='Active' AND department IS NOT NULL ORDER BY department"
        ).fetchall()]
        ot_rows = list(reversed(ot_rows))
        labels = [f"{mnames[(int(r['mn']) or 1)-1]} {r['yr']}" for r in ot_rows]
        if metric == "ot_hours":
            values = [round(float(r["total_ot"] or 0)/60, 1) for r in ot_rows]
        else:  # ot_amount from salary_records — conn still open
            sal_rows = conn.execute(f"""
                SELECT s.month as mn, s.year as yr,
                       SUM(COALESCE(s.ot_amount,0)) as total_ot_amt,
                       COUNT(DISTINCT s.emp_code) as emp_c
                FROM salary_records s JOIN employees e ON s.emp_code=e.emp_code
                WHERE s.ot_amount>0{extra}
                GROUP BY s.year, s.month
                ORDER BY s.year DESC, s.month DESC LIMIT ?
            """, params+[months_back]).fetchall()
            sal_rows = list(reversed(sal_rows))
            sal_map = {f"{mnames[(r['mn'] or 1)-1]} {r['yr']}": float(r["total_ot_amt"] or 0)
                       for r in sal_rows}
            values = [round(sal_map.get(lbl, 0), 2) for lbl in labels]
        conn.close()
        emp_counts = [r["emp_count"] for r in ot_rows]
        return jsonify({"success":True,"labels":labels,"values":values,
            "emp_counts":emp_counts,"metric":metric,"departments":depts})

    # Get last N months of salary data
    rows = conn.execute(f"""
        SELECT s.month, s.year,
               SUM(s.net_salary) as net_salary,
               SUM(s.gross) as gross,
               SUM(s.pf) as pf,
               SUM(s.esi) as esi,
               SUM(s.present_days) as present_days,
               COUNT(DISTINCT s.emp_code) as emp_count
        FROM salary_records s JOIN employees e ON s.emp_code=e.emp_code
        WHERE 1=1{extra}
        GROUP BY s.year, s.month
        ORDER BY s.year DESC, s.month DESC
        LIMIT ?
    """, params+[months_back]).fetchall()

    depts = [d["department"] for d in conn.execute(
        "SELECT DISTINCT department FROM employees WHERE status='Active' AND department IS NOT NULL ORDER BY department"
    ).fetchall()]
    conn.close()

    rows = list(reversed(rows))
    labels = [f"{mnames[(r['month'] or 1)-1]} {r['year']}" for r in rows]
    values = [round(float(r[metric] or 0),2) for r in rows]
    emp_counts = [r["emp_count"] for r in rows]

    return jsonify({"success":True,
        "labels":labels, "values":values,
        "emp_counts":emp_counts,
        "metric":metric,
        "departments":depts})

@app.route("/api/absence-trend")
@amgr
def absence_trend():
    """Absence trend data for dashboard chart — date range, emp, dept filters"""
    from_date = request.args.get("from","")
    to_date   = request.args.get("to","")
    emp_code  = request.args.get("emp","")
    dept      = request.args.get("dept","")
    # Default: last 30 days
    from datetime import date as _dt, timedelta as _td
    if not from_date:
        from_date = (_dt.today() - _td(days=29)).strftime("%Y-%m-%d")
    if not to_date:
        to_date = _dt.today().strftime("%Y-%m-%d")
    conn = get_db()
    sql = """SELECT a.att_date, COUNT(*) as absent_count
        FROM attendance a JOIN employees e ON a.emp_code=e.emp_code
        WHERE a.att_date BETWEEN ? AND ? AND a.status='Absent' AND e.status='Active'"""
    params = [from_date, to_date]
    if emp_code: sql += " AND a.emp_code=?"; params.append(emp_code)
    if dept:     sql += " AND e.department=?"; params.append(dept)
    sql += " GROUP BY a.att_date ORDER BY a.att_date"
    rows = conn.execute(sql, params).fetchall()
    depts = [d["department"] for d in conn.execute(
        "SELECT DISTINCT department FROM employees WHERE status='Active' AND department IS NOT NULL ORDER BY department"
    ).fetchall()]
    emps = conn.execute("SELECT emp_code,emp_name FROM employees WHERE status='Active' ORDER BY emp_name").fetchall()
    conn.close()
    return jsonify({"success":True,
        "labels":[r["att_date"] for r in rows],
        "values":[r["absent_count"] for r in rows],
        "departments":depts,
        "from_date":from_date,"to_date":to_date})

@app.route("/dashboard")
@amgr
def dashboard():
    conn=get_db(); m,y=date.today().month,date.today().year
    today_str=date.today().strftime("%Y-%m-%d")
    month_str=f"{m:02d}"
    # Ensure dept_access exists in session
    if "dept_access" not in session:
        session["dept_access"] = []

    # Active employees — filtered by user's dept access
    d_sql, d_params = get_user_dept_filter("e")
    total = conn.execute(f"SELECT COUNT(*) as c FROM employees e WHERE status='Active'{d_sql}", d_params).fetchone()["c"]
    staff = conn.execute(f"SELECT COUNT(*) as c FROM employees e WHERE category='Staff' AND status='Active'{d_sql}", d_params).fetchone()["c"]
    assoc = conn.execute(f"SELECT COUNT(*) as c FROM employees e WHERE category='Associate' AND status='Active'{d_sql}", d_params).fetchone()["c"]

    # Today present/absent counts
    present_staff = conn.execute(f"""SELECT COUNT(DISTINCT a.emp_code) as c FROM attendance a
        JOIN employees e ON a.emp_code=e.emp_code
        WHERE a.att_date=? AND e.category='Staff' AND a.status NOT IN ('Absent','WO') AND e.status='Active'{d_sql}""",
        [today_str]+d_params).fetchone()["c"]
    present_assoc = conn.execute(f"""SELECT COUNT(DISTINCT a.emp_code) as c FROM attendance a
        JOIN employees e ON a.emp_code=e.emp_code
        WHERE a.att_date=? AND e.category='Associate' AND a.status NOT IN ('Absent','WO') AND e.status='Active'{d_sql}""",
        [today_str]+d_params).fetchone()["c"]

    # Count employees who have WO today (weekly off day) — exclude from absent
    _today_dow = date.today().weekday()  # 0=Mon,6=Sun
    _wo_day_names = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
    _today_wo_name = _wo_day_names[_today_dow]
    wo_staff_today = conn.execute(f"""SELECT COUNT(*) as c FROM employees e
        WHERE e.category='Staff' AND e.status='Active'
        AND COALESCE(e.weekly_off,'Sunday')=?{d_sql}""",
        [_today_wo_name]+d_params).fetchone()["c"]
    wo_assoc_today = conn.execute(f"""SELECT COUNT(*) as c FROM employees e
        WHERE e.category!='Staff' AND e.status='Active'
        AND COALESCE(e.weekly_off,'Sunday')=?{d_sql}""",
        [_today_wo_name]+d_params).fetchone()["c"]

    # ── Yet-to-Arrive Guard for Dashboard ───────────────────────────────
    # ALL employees whose assigned shift start time + 30 min grace has NOT passed yet
    # should NOT be counted as Absent. They get their own KPI card.
    from datetime import datetime as _dtnow_db
    _now_min_db = _dtnow_db.now().hour * 60 + _dtnow_db.now().minute
    _GRACE = 30  # minutes grace period after shift start before marking absent

    # Get ALL employees with ANY shift assigned today (not just night shifts)
    all_shifts_today = conn.execute("""
        SELECT srd.emp_code, s.start_time, s.shift_name,
               e.emp_name, e.category, e.department
        FROM shift_roster_dates srd
        JOIN shifts s ON srd.shift_id = s.id
        JOIN employees e ON srd.emp_code = e.emp_code
        WHERE srd.shift_date = ? AND e.status='Active'
    """, (today_str,)).fetchall()

    not_started_staff = 0
    not_started_assoc = 0
    yet_to_arrive_list = []  # For new KPI card

    _wo_day_name_set = _wo_day_names[_today_dow]  # today's day name for WO check
    for ns in all_shifts_today:
        ec = ns["emp_code"]
        # Skip if today is employee's weekly off — WO is handled separately
        emp_wo = conn.execute(
            "SELECT COALESCE(weekly_off,'Sunday') as wo FROM employees WHERE emp_code=?",
            (ec,)).fetchone()
        if emp_wo and emp_wo["wo"] == _wo_day_name_set:
            continue  # WO today — don't double-count in yet_to_arrive
        try:
            sh_h, sh_m = map(int, (ns["start_time"] or "09:00").split(":")[:2])
            sh_min = sh_h * 60 + sh_m
        except:
            sh_min = 9 * 60

        # Not yet arrived = shift start + grace hasn't passed
        if _now_min_db < sh_min + _GRACE:
            has_att = conn.execute(
                "SELECT status FROM attendance WHERE emp_code=? AND att_date=?",
                (ec, today_str)).fetchone()
            if not has_att or has_att["status"] in ("Absent", "WO"):
                yet_to_arrive_list.append({
                    "emp_code": ec,
                    "emp_name": ns["emp_name"],
                    "category": ns["category"],
                    "department": ns["department"],
                    "shift_name": ns["shift_name"],
                    "shift_start": ns["start_time"],
                })
                if ns["category"] == "Staff":
                    not_started_staff += 1
                else:
                    not_started_assoc += 1

    absent_staff = max(0, staff - present_staff - not_started_staff - wo_staff_today)
    absent_assoc = max(0, assoc - present_assoc - not_started_assoc - wo_assoc_today)
    yet_to_arrive = not_started_staff + not_started_assoc

    # ── WOP (Week Off Present) Today ──────────────────────────────────
    wop_today_list = []
    wop_rows = conn.execute("""
        SELECT a.emp_code, e.emp_name, e.department, e.category,
               a.in_time, a.out_time
        FROM attendance a JOIN employees e ON a.emp_code=e.emp_code
        WHERE a.att_date=? AND a.status='WOP' AND e.status='Active'
        ORDER BY e.department, e.emp_name
    """, (today_str,)).fetchall()
    for wr in wop_rows:
        wop_today_list.append({
            "emp_code": wr["emp_code"], "emp_name": wr["emp_name"],
            "category": wr["category"], "department": wr["department"] or "",
            "in_time": wr["in_time"] or "", "out_time": wr["out_time"] or ""
        })

    # ── Week Off & Absent (WO day but did NOT come) ──────────────────
    wo_absent_list = []
    _wo_name_map = {0:"Monday",1:"Tuesday",2:"Wednesday",3:"Thursday",4:"Friday",5:"Saturday",6:"Sunday"}
    _today_wo_name = _wo_name_map[date.today().weekday()]
    wo_absent_rows = conn.execute("""
        SELECT emp_code, emp_name, department, category,
               COALESCE(weekly_off,'Sunday') as weekly_off
        FROM employees WHERE status='Active'
          AND COALESCE(weekly_off,'Sunday')=?
        ORDER BY department, emp_name
    """, (_today_wo_name,)).fetchall()
    # Only exclude employees who ACTUALLY came (WOP = Week Off but Present)
    # WO, Absent, or no record = did not come = show in WO & Absent
    _came_today = {r["emp_code"] for r in conn.execute(
        "SELECT emp_code FROM attendance WHERE att_date=? AND status IN ('Present','WOP','HP','Miss Punch','Leave','Half Day')",
        (today_str,)).fetchall()}
    for wr in wo_absent_rows:
        if wr["emp_code"] not in _came_today:
            wo_absent_list.append({
                "emp_code": wr["emp_code"], "emp_name": wr["emp_name"],
                "category": wr["category"], "department": wr["department"] or ""
            })

    # ── Leave Approved Today ──────────────────────────────────────────
    leave_today_list = []
    leave_rows = conn.execute("""
        SELECT lr.emp_code, e.emp_name, e.department, e.category,
               lr.leave_type, lr.from_date, lr.to_date, lr.reason
        FROM leave_requests lr JOIN employees e ON lr.emp_code=e.emp_code
        WHERE lr.status='Approved'
          AND lr.from_date <= ? AND lr.to_date >= ?
          AND e.status='Active'
        ORDER BY e.department, e.emp_name
    """, (today_str, today_str)).fetchall()
    for lr in leave_rows:
        leave_today_list.append({
            "emp_code": lr["emp_code"], "emp_name": lr["emp_name"],
            "category": lr["category"], "department": lr["department"] or "",
            "leave_type": lr["leave_type"] or "Leave",
            "from_date": lr["from_date"], "to_date": lr["to_date"]
        })

    # WOP today — employees who punched on their weekly off day
    wop_today = conn.execute(f"""SELECT COUNT(DISTINCT a.emp_code) FROM attendance a
        JOIN employees e ON a.emp_code=e.emp_code
        WHERE a.att_date=? AND a.status='WOP' AND e.status='Active'{d_sql}""",
        [today_str]+d_params).fetchone()[0]
    wop_list = conn.execute(f"""SELECT e.emp_code, e.emp_name, e.department, e.category,
        a.in_time, a.out_time, a.ot_minutes,
        COALESCE(srd_shift.shift_name,'—') as shift_name
        FROM attendance a JOIN employees e ON a.emp_code=e.emp_code
        LEFT JOIN shift_roster_dates srd ON srd.emp_code=a.emp_code AND srd.shift_date=a.att_date
        LEFT JOIN shifts srd_shift ON srd_shift.id=srd.shift_id
        WHERE a.att_date=? AND a.status='WOP' AND e.status='Active'{d_sql}
        ORDER BY e.department, e.emp_name""",
        [today_str]+d_params).fetchall()

    # OT this month (1st to yesterday)
    ot_staff = 0  # Not shown on dashboard
    ot_assoc = conn.execute(f"""SELECT COALESCE(SUM(a.ot_minutes),0) as s FROM attendance a
        JOIN employees e ON a.emp_code=e.emp_code
        WHERE strftime('%m',a.att_date)=? AND strftime('%Y',a.att_date)=?
        AND a.att_date < ? AND e.category='Associate' AND e.status='Active'{d_sql}""",
        [month_str,str(y),today_str]+d_params).fetchone()["s"]

    # Dept-wise data for dashboard drilldown
    dept_active = conn.execute(f"""SELECT department,
        SUM(CASE WHEN category='Staff' THEN 1 ELSE 0 END) as staff_count,
        SUM(CASE WHEN category='Associate' THEN 1 ELSE 0 END) as assoc_count
        FROM employees e WHERE status='Active'{d_sql} GROUP BY department ORDER BY department""",
        d_params).fetchall()

    dept_present = conn.execute(f"""SELECT e.department,
        SUM(CASE WHEN e.category='Staff' THEN 1 ELSE 0 END) as staff_p,
        SUM(CASE WHEN e.category='Associate' THEN 1 ELSE 0 END) as assoc_p
        FROM attendance a JOIN employees e ON a.emp_code=e.emp_code
        WHERE a.att_date=? AND a.status NOT IN ('Absent','WO') AND e.status='Active'{d_sql}
        GROUP BY e.department ORDER BY e.department""", [today_str]+d_params).fetchall()

    dept_ot = conn.execute(f"""SELECT e.department,
        SUM(CASE WHEN e.category='Staff' THEN COALESCE(a.ot_minutes,0) ELSE 0 END) as staff_ot,
        SUM(CASE WHEN e.category='Associate' THEN COALESCE(a.ot_minutes,0) ELSE 0 END) as assoc_ot
        FROM attendance a JOIN employees e ON a.emp_code=e.emp_code
        WHERE strftime('%m',a.att_date)=? AND strftime('%Y',a.att_date)=?
        AND a.att_date < ? AND e.status='Active'{d_sql}
        GROUP BY e.department ORDER BY e.department""", [month_str,str(y),today_str]+d_params).fetchall()

    # Manpower master
    if d_params:
        mp_sql = f"SELECT * FROM dept_manpower WHERE department IN ({','.join(['?']*len(d_params))}) ORDER BY department"
        manpower = conn.execute(mp_sql, d_params).fetchall()
    else:
        manpower = conn.execute("SELECT * FROM dept_manpower ORDER BY department").fetchall() if conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='dept_manpower'").fetchone() else []

    payout=conn.execute("SELECT COALESCE(SUM(net_salary),0) as s FROM salary_records WHERE month=? AND year=?",(m,y)).fetchone()["s"]
    recent=conn.execute("SELECT * FROM employees WHERE status='Active' ORDER BY id DESC LIMIT 8").fetchall()
    conn.close()
    cel=get_celebrations()
    return render_template("dashboard.html",total=total,staff=staff,assoc=assoc,
        present_staff=present_staff, present_assoc=present_assoc,
        absent_staff=absent_staff, absent_assoc=absent_assoc,
        yet_to_arrive=yet_to_arrive,
        yet_to_arrive_list=yet_to_arrive_list,
        wop_today=len(wop_today_list),
        wop_today_list=wop_today_list,
        wo_absent=len(wo_absent_list),
        wo_absent_list=wo_absent_list,
        leave_today=len(leave_today_list),
        leave_today_list=leave_today_list,
        ot_staff_hrs=round(ot_staff/60,1), ot_assoc_hrs=round(ot_assoc/60,1),
        dept_active=[dict(d) for d in dept_active],
        dept_present=[dict(d) for d in dept_present],
        dept_ot=[dict(d) for d in dept_ot],
        manpower=[dict(m2) for m2 in manpower],
        payout=payout,recent=recent,
        today=date.today().strftime("%d %B %Y"),month_name=MONTHS[m-1],year=y,
        current_month=m, current_year=y, months=MONTHS,
        celebrations=cel)

@app.route("/employees")
@amgr
def employees():
    q   = request.args.get("q","")
    st  = request.args.get("status","Active")
    cat = request.args.get("cat","")
    conn = get_db()
    sql = "SELECT * FROM employees WHERE 1=1"
    params = []
    if st:  sql += " AND status=?";   params.append(st)
    if cat == "Staff":     sql += " AND category='Staff'"
    elif cat == "NonStaff": sql += " AND category!='Staff'"
    if q:   sql += " AND (emp_name LIKE ? OR emp_code LIKE ? OR department LIKE ?)"; params.extend([f"%{q}%",f"%{q}%",f"%{q}%"])
    # Dept access restriction — use alias "e" then strip it for direct table query
    d_sql_emp, d_params_emp = get_user_dept_filter("e")
    if d_sql_emp: sql += d_sql_emp.replace("e.department","department"); params.extend(d_params_emp)
    sql += " ORDER BY category,emp_name"
    emps = conn.execute(sql, params).fetchall()
    depts = conn.execute("SELECT DISTINCT department FROM employees WHERE department IS NOT NULL ORDER BY department").fetchall()
    staff_count    = conn.execute("SELECT COUNT(*) as c FROM employees WHERE category='Staff' AND status='Active'").fetchone()["c"]
    nonstaff_count = conn.execute("SELECT COUNT(*) as c FROM employees WHERE category!='Staff' AND status='Active'").fetchone()["c"]
    # Get masters for dropdowns
    master_cats   = [r["name"] for r in conn.execute("SELECT name FROM master_categories   WHERE is_active=1 ORDER BY name").fetchall()]
    master_depts  = [r["name"] for r in conn.execute("SELECT name FROM master_departments  WHERE is_active=1 ORDER BY name").fetchall()]
    master_desigs = [r["name"] for r in conn.execute("SELECT name FROM master_designations WHERE is_active=1 ORDER BY name").fetchall()]
    master_locs   = [r["name"] for r in conn.execute("SELECT name FROM master_locations    WHERE is_active=1 ORDER BY name").fetchall()]
    # Load custom fields
    try:
        custom_fields = conn.execute("SELECT * FROM employee_custom_fields WHERE is_active=1 ORDER BY display_order,field_label").fetchall()
        custom_fields = [dict(f) for f in custom_fields]
    except:
        custom_fields = []
    conn.close()
    return render_template("employees.html", employees=emps, search=q, status_filter=st,
        cat_filter=cat, departments=[d[0] for d in depts],
        staff_count=staff_count, nonstaff_count=nonstaff_count,
        master_cats=master_cats, master_depts=master_depts,
        master_desigs=master_desigs, master_locs=master_locs,
        custom_fields=custom_fields)

@app.route("/employees/add",methods=["POST"])
@amgr
def add_emp():
    # Accept both JSON and form data
    if request.is_json:
        d = request.json
    else:
        d = request.form
    conn=get_db()
    try:
        basic=float(d.get("basic",0) or 0)
        hra=float(d.get("hra",0) or 0)
        special=float(d.get("special_allowance",0) or 0)
        _sid = d.get("scheme_id","") or ""; scheme_id = int(_sid) if _sid and _sid not in ("null","None","0","") else None
        conn.execute("""INSERT INTO employees (emp_code,emp_name,category,department,location,designation,
            date_of_joining,date_of_birth,gender,phone,email,personal_email,official_email,address,aadhar,pan,
            bank_account,bank_name,ifsc,basic,hra,special_allowance,
            pf_applicable,esi_applicable,tds_percent,scheme_id,
            uan_number,pf_number,esic_number,weekly_off,status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'Active')""",
            (d["emp_code"],d["emp_name"],d["category"],d.get("department",""),d.get("location",""),d.get("designation",""),
             d.get("date_of_joining",""),d.get("date_of_birth",""),d.get("gender",""),
             d.get("phone",""),d.get("email",""),d.get("personal_email",""),d.get("official_email",""),
             d.get("address",""),d.get("aadhar",""),d.get("pan",""),
             d.get("bank_account",""),d.get("bank_name",""),d.get("ifsc",""),
             basic,hra,special,
             1 if d.get("pf_applicable") else 0, 1 if d.get("esi_applicable") else 0,
             float(d.get("tds_percent",0) or 0), scheme_id,
             d.get("uan_number",""),d.get("pf_number",""),d.get("esic_number",""),
             d.get("weekly_off","Sunday")))
        conn.execute("INSERT OR IGNORE INTO users (username,password,role,emp_id,name) VALUES (?,?,?,?,?)",
                     (d["emp_code"].lower(),hp("Emp@123"),"employee",d["emp_code"],d["emp_name"]))
        conn.commit(); return jsonify({"success":True})
    except Exception as e: return jsonify({"success":False,"error":str(e)})
    finally: conn.close()

@app.route("/employees/edit/<emp_code>",methods=["POST"])
@amgr
def edit_emp(emp_code):
    if request.is_json:
        d = request.json
    else:
        d = request.form
    conn=get_db()
    try:
        basic=float(d.get("basic",0) or 0)
        hra=float(d.get("hra",0) or 0)
        special=float(d.get("special_allowance",0) or 0)
        _sid = d.get("scheme_id","") or ""; scheme_id = int(_sid) if _sid and _sid not in ("null","None","0","") else None
        # ── Track department change ──────────────────────────
        old_emp = conn.execute("SELECT department, emp_name FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
        old_dept = old_emp["department"] if old_emp else ""
        new_dept = d.get("department","")
        if old_dept != new_dept and new_dept:
            conn.execute("""INSERT INTO emp_dept_history
                (emp_code,emp_name,old_department,new_department,changed_on,changed_by)
                VALUES (?,?,?,?,?,?)""",
                (emp_code, d.get("emp_name",""), old_dept, new_dept,
                 datetime.now().strftime("%Y-%m-%d %H:%M"),
                 session.get("username","Admin")))
        conn.execute("""UPDATE employees SET emp_name=?,category=?,department=?,location=?,designation=?,
            date_of_joining=?,date_of_birth=?,gender=?,phone=?,email=?,personal_email=?,official_email=?,
            address=?,aadhar=?,pan=?,
            bank_account=?,bank_name=?,ifsc=?,basic=?,hra=?,special_allowance=?,
            pf_applicable=?,esi_applicable=?,tds_percent=?,scheme_id=?,
            uan_number=?,pf_number=?,esic_number=?,weekly_off=?,status=? WHERE emp_code=?""",
            (d["emp_name"],d["category"],new_dept,d.get("location",""),d.get("designation",""),
             d.get("date_of_joining",""),d.get("date_of_birth",""),d.get("gender",""),
             d.get("phone",""),d.get("email",""),d.get("personal_email",""),d.get("official_email",""),
             d.get("address",""),d.get("aadhar",""),d.get("pan",""),
             d.get("bank_account",""),d.get("bank_name",""),d.get("ifsc",""),
             basic,hra,special,
             1 if d.get("pf_applicable") else 0, 1 if d.get("esi_applicable") else 0,
             float(d.get("tds_percent",0) or 0), scheme_id,
             d.get("uan_number",""),d.get("pf_number",""),d.get("esic_number",""),
             d.get("weekly_off","Sunday"),
             d.get("status","Active"),emp_code))
        conn.commit(); return jsonify({"success":True})
    except Exception as e: return jsonify({"success":False,"error":str(e)})
    finally: conn.close()

@app.route("/employees/get/<emp_code>")
@amgr
def get_emp(emp_code):
    conn=get_db(); e=conn.execute("SELECT * FROM employees WHERE emp_code=?",(emp_code,)).fetchone()
    conn.close(); return jsonify(dict(e)) if e else jsonify({})

@app.route("/employees/delete/<emp_code>",methods=["POST"])
@amgr
def del_emp(emp_code):
    conn=get_db(); conn.execute("DELETE FROM employees WHERE emp_code=?",(emp_code,))
    conn.commit(); conn.close(); return jsonify({"success":True})

@app.route("/employees/calculate-salary-defaults",methods=["POST"])
@amgr
def calc_defaults():
    basic=float(request.json.get("basic",0) or 0)
    return jsonify({"hra":round(basic*HRA_PCT,2),"special":round(basic*SPECIAL_PCT,2),"gross":round(basic*(1+HRA_PCT+SPECIAL_PCT),2)})

@app.route("/employees/upload-excel",methods=["POST"])
@amgr
def upload_emp_excel():
    if "file" not in request.files: return jsonify({"success":False,"error":"No file"})
    try:
        import openpyxl
        wb=openpyxl.load_workbook(io.BytesIO(request.files["file"].read())); ws=wb.active
        # Auto-detect header row — scan first 5 rows
        hdrs = []; data_start = 2
        for row_num in range(1, 6):
            # Clean header: lowercase, replace spaces with _, remove . : * extra chars
            row_vals = []
            for c in ws[row_num]:
                if c.value:
                    h = str(c.value).strip().lower()
                    h = h.replace(" ","_").replace(".","").replace(":","").replace("*","").replace("(","").replace(")","")
                    h = h.strip("_")
                    row_vals.append(h)
                else:
                    row_vals.append("")
            if "emp_code" in row_vals or "emp_name" in row_vals or "empcode" in row_vals or "name_of_employee" in row_vals:
                hdrs = row_vals
                data_start = row_num + 1
                break
        if not hdrs:
            hdrs = []
            for c in ws[1]:
                if c.value:
                    h = str(c.value).strip().lower()
                    h = h.replace(" ","_").replace(".","").replace(":","").replace("*","").replace("(","").replace(")","")
                    h = h.strip("_")
                    hdrs.append(h)
                else:
                    hdrs.append("")
            data_start = 2
        conn=get_db(); added=updated=0
        for row in ws.iter_rows(min_row=data_start,values_only=True):
            if not any(row): continue
            d=dict(zip(hdrs,row))

            # Support YOUR exact format: EMP CODE, NAME OF EMPLOYEE, CATEGORY, LOCATION, DESIGNATION etc.
            # Employee codes are stored exactly as in Excel (numbers like 1001, 0004 etc.)
            raw_code = (d.get("emp_code") or d.get("empcode") or d.get("emp_code_") or 
                        d.get("employee_code") or d.get("emp_id") or "")
            if raw_code is None: raw_code = ""
            ecode = str(int(float(str(raw_code)))).strip() if raw_code and str(raw_code).strip().replace(".","").isdigit() else str(raw_code).strip()
            ename = str(d.get("emp_name") or d.get("name_of_employee") or d.get("name_of_employee_") or 
                        d.get("name") or d.get("employee_name") or "").strip()
            if not ecode or not ename: continue

            # Category: Map old values to Staff/Associate
            raw_cat = str(d.get("category","Associate") or "Associate").strip()
            # Map VPL/PT/MMSKY/Un-Registered → Associate, keep Staff as Staff
            cat = "Staff" if raw_cat.lower() in ["staff","salaried"] else "Associate"

            # Location: HALL - 1, HALL - 2, STAFF, CANTEEN etc.
            loc   = str(d.get("location") or d.get("location_") or d.get("dept") or "").strip()

            # Department = Location (your format uses LOCATION as department)
            dept  = loc or str(d.get("department","") or "").strip()

            desig = str(d.get("designation","") or d.get("designation_","") or "").strip()

            # DOJ - handle datetime objects
            doj_raw = d.get("date_of_joining","") or d.get("doj","") or d.get("joining_date","") or ""
            if hasattr(doj_raw, 'strftime'): doj = doj_raw.strftime("%Y-%m-%d")
            else: doj = str(doj_raw).strip()[:10] if doj_raw else ""

            # DOB - handle datetime objects
            dob_raw = d.get("date_of_birth","") or d.get("dob_dd/mm/yyyy","") or d.get("dob","") or ""
            if hasattr(dob_raw, 'strftime'): dob = dob_raw.strftime("%Y-%m-%d")
            else: dob = str(dob_raw).strip()[:10] if dob_raw else ""

            # Salary
            basic   = float(d.get("basic",0) or 0)
            hra     = float(d.get("hra",0) or 0)
            special = float(d.get("special_allowance",0) or d.get("special",0) or 0)
            pf  = 1 if str(d.get("pf_applicable",d.get("pf","yes")) or "yes").lower() not in ["no","0","false"] else 0
            esi = 1 if str(d.get("esic_applicable",d.get("esi","yes")) or "yes").lower() not in ["no","0","false"] else 0
            tds_pct = float(d.get("tds%",d.get("tds_percent",0)) or 0)
            gender = str(d.get("gender","") or "").strip()
            aadhar = str(d.get("aadhar","") or "").strip()
            # Scheme — match by name
            scheme_name = str(d.get("scheme","") or "").strip()
            scheme_id = None
            if scheme_name:
                sr = conn.execute("SELECT id FROM employee_schemes WHERE scheme_name=? AND is_active=1", (scheme_name,)).fetchone()
                if sr: scheme_id = sr["id"]

            # Contact
            phone   = str(d.get("phone") or d.get("contact_number") or d.get("contact") or d.get("mobile") or "").strip()

            # Email fields - your format has Official Email ID and Personal mail ID
            email          = str(d.get("email") or d.get("offical_email_id") or d.get("official_email_id") or d.get("official_email") or "").strip()
            official_email = str(d.get("official_email") or d.get("offical_email_id") or d.get("official_email_id") or "").strip()
            personal_email = str(d.get("personal_email") or d.get("personal_mail_id") or d.get("personal_mail") or "").strip()
            if not email and official_email: email = official_email
            elif not email and personal_email: email = personal_email

            # Use official email as primary if available
            if not email and official_email: email = official_email
            elif not email and personal_email: email = personal_email

            pan     = str(d.get("pan","") or "").strip()
            bank    = str(d.get("bank_account") or d.get("bank_a/c_no") or d.get("bank_ac_no") or d.get("account_no") or "").strip()
            bank_nm = str(d.get("bank_name") or "").strip()
            ifsc    = str(d.get("ifsc") or d.get("ifsc_code") or "").strip()
            uan_no  = str(d.get("uan_number") or d.get("uan_no") or d.get("uan") or "").strip()
            pf_no   = str(d.get("pf_number") or d.get("pf_no") or d.get("pf_account") or "").strip()
            esic_no = str(d.get("esic_number") or d.get("esic_no") or d.get("esic_ip_no") or "").strip()
            wo_raw = str(d.get("weekly_off","") or "").strip()
            valid_days = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
            weekly_off = next((day for day in valid_days if day.lower() == wo_raw.lower()), "Sunday")
            for _col, _def in [("uan_number","TEXT"),("pf_number","TEXT"),("esic_number","TEXT")]:
                try: conn.execute(f"ALTER TABLE employees ADD COLUMN {_col} {_def}")
                except: pass
            if conn.execute("SELECT id FROM employees WHERE emp_code=?",(ecode,)).fetchone():
            
                conn.execute("""UPDATE employees SET emp_name=?,category=?,department=?,location=?,designation=?,
                    date_of_joining=?,date_of_birth=?,gender=?,basic=?,hra=?,special_allowance=?,
                    phone=?,email=?,official_email=?,personal_email=?,pan=?,aadhar=?,
                    bank_account=?,bank_name=?,ifsc=?,pf_applicable=?,esi_applicable=?,tds_percent=?,scheme_id=?,
                    uan_number=?,pf_number=?,esic_number=?,weekly_off=?
                    WHERE emp_code=?""",
                    (ename,cat,dept,loc,desig,doj,dob,gender,basic,hra,special,
                     phone,email,official_email,personal_email,pan,aadhar,
                     bank,bank_nm,ifsc,pf,esi,tds_pct,scheme_id,
                     uan_no,pf_no,esic_no,weekly_off,ecode))
                updated+=1
            else:
                conn.execute("""INSERT INTO employees (emp_code,emp_name,category,department,location,designation,
                    date_of_joining,date_of_birth,gender,basic,hra,special_allowance,phone,email,official_email,personal_email,pan,aadhar,
                    bank_account,bank_name,ifsc,pf_applicable,esi_applicable,tds_percent,scheme_id,
                    uan_number,pf_number,esic_number,weekly_off,status)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'Active')""",
                    (ecode,ename,cat,dept,loc,desig,doj,dob,gender,basic,hra,special,
                     phone,email,official_email,personal_email,pan,aadhar,
                     bank,bank_nm,ifsc,pf,esi,tds_pct,scheme_id,
                     uan_no,pf_no,esic_no,weekly_off))
                conn.execute("INSERT OR IGNORE INTO users (username,password,role,emp_id,name) VALUES (?,?,?,?,?)",
                             (ecode.lower(),hp("Emp@123"),"employee",ecode,ename))
                added+=1

            # Save custom field values from import row
            try:
                custom_field_defs = conn.execute(
                    "SELECT field_name,field_label FROM employee_custom_fields WHERE is_active=1 AND in_export=1 ORDER BY display_order"
                ).fetchall()
                for cfd in custom_field_defs:
                    fname_key = cfd["field_name"]
                    flabel_key = cfd["field_label"].lower().replace(" ","_").replace("-","_")
                    val = str(d.get(fname_key) or d.get(flabel_key) or d.get(cfd["field_label"]) or "").strip()
                    if val:
                        conn.execute("""INSERT OR REPLACE INTO employee_custom_values (emp_code,field_name,field_value)
                            VALUES (?,?,?)""",
                            (ecode, fname_key, val))
            except: pass

        conn.commit(); conn.close()
        return jsonify({"success":True,"added":added,"updated":updated})
    except Exception as e: return jsonify({"success":False,"error":str(e)})

# ─── ATTENDANCE ──────────────────────────────────────

# ── Late Employee Management ──────────────────────
@app.route("/attendance/late-report")
@amgr
def late_report():
    m      = int(request.args.get("month", date.today().month))
    y      = int(request.args.get("year",  date.today().year))
    dept   = request.args.get("dept","")
    cat    = request.args.get("cat","")
    status_filter = request.args.get("status","pending")  # pending | approved | all
    conn   = get_db()

    extra  = ""
    params = [f"{m:02d}", str(y)]
    if dept: extra += " AND e.department=?"; params.append(dept)
    if cat:  extra += " AND e.category=?";   params.append(cat)

    # Status filter
    if status_filter == "pending":
        status_extra = " AND (a.late_waived IS NULL OR a.late_waived = 0)"
        late_filter  = " AND a.late_minutes > 0"
    elif status_filter == "approved":
        status_extra = " AND a.late_waived = 1"
        late_filter  = ""  # late_minutes=0 after waive — don't filter it out
    else:  # all
        status_extra = ""
        late_filter  = " AND (a.late_minutes > 0 OR a.late_waived = 1)"

    sql = f"""SELECT a.*, e.emp_name, e.department, e.category, e.designation
        FROM attendance a JOIN employees e ON a.emp_code=e.emp_code
        WHERE strftime('%m',a.att_date)=? AND strftime('%Y',a.att_date)=?
        {late_filter}{status_extra}{extra}
        ORDER BY a.att_date, e.emp_name"""

    rows  = conn.execute(sql, params).fetchall()
    depts = conn.execute("SELECT DISTINCT department FROM employees WHERE status='Active' ORDER BY department").fetchall()
    conn.close()

    by_emp = {}
    for r in rows:
        ec = r["emp_code"]
        if ec not in by_emp:
            by_emp[ec] = {"emp_name":r["emp_name"],"department":r["department"],
                         "category":r["category"],"records":[]}
        by_emp[ec]["records"].append(dict(r))

    return render_template("late_report.html",
        by_emp=by_emp, month=m, year=y,
        month_name=MONTHS[m-1], months=MONTHS,
        departments=[d["department"] for d in depts],
        selected_dept=dept, selected_cat=cat,
        status_filter=status_filter)

@app.route("/attendance/late-waive", methods=["POST"])
@amgr
def late_waive():
    """Waive late mark for an attendance record — permanently protected from recalculate"""
    d = request.json; conn = get_db()
    try:
        emp_code = d.get("emp_code")
        att_date = d.get("att_date")
        reason   = d.get("reason","Company work")
        # Set late_minutes=0, late_waived=1 — recalculate will never overwrite this
        conn.execute("""UPDATE attendance
            SET late_minutes=0, late_waived=1, remarks=?
            WHERE emp_code=? AND att_date=?""",
            (f"Late waived: {reason}", emp_code, att_date))
        conn.commit()
        return jsonify({"success":True})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})
    finally: conn.close()

@app.route("/attendance/late-revert", methods=["POST"])
@amgr
def late_revert():
    """Revert a waived late mark back to pending (late_waived=0, restore late_minutes)"""
    d = request.json; conn = get_db()
    try:
        emp_code = d.get("emp_code")
        att_date = d.get("att_date")
        # Recalculate late_minutes fresh from punch data
        rec = conn.execute(
            "SELECT in_time, out_time FROM attendance WHERE emp_code=? AND att_date=?",
            (emp_code, att_date)).fetchone()
        if not rec:
            return jsonify({"success": False, "error": "Record not found"})
        emp = conn.execute("SELECT category FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
        category = emp["category"] if emp else "Associate"
        c = calc_att(emp_code, rec["in_time"] or None, rec["out_time"] or None, category)
        late_min = c.get("late_minutes", 0) or 0
        conn.execute("""UPDATE attendance
            SET late_waived=0, late_minutes=?, remarks=NULL
            WHERE emp_code=? AND att_date=?""",
            (late_min, emp_code, att_date))
        conn.commit()
        return jsonify({"success": True, "late_minutes": late_min})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
    finally: conn.close()


@app.route("/attendance/late-bulk-waive", methods=["POST"])
@amgr
def late_bulk_waive():
    """Waive multiple late marks — permanently protected"""
    d = request.json; conn = get_db()
    try:
        records = d.get("records",[])
        reason  = d.get("reason","Company work")
        for rec in records:
            conn.execute("""UPDATE attendance
                SET late_minutes=0, late_waived=1, remarks=?
                WHERE emp_code=? AND att_date=?""",
                (f"Late waived: {reason}", rec["emp_code"], rec["att_date"]))
        conn.commit()
        return jsonify({"success":True,"waived":len(records)})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})
    finally: conn.close()


# ── Manual Attendance Entry ──────────────────────────────────

@app.route("/attendance/edit-record", methods=["POST"])
@amgr
def attendance_edit_record():
    """Edit/Add attendance record for any date with reason"""
    d = request.json
    conn = get_db()
    try:
        emp_code  = str(d.get("emp_code","")).strip()
        att_date  = str(d.get("att_date","")).strip()
        in_time   = str(d.get("in_time","")).strip() or None
        out_time  = str(d.get("out_time","")).strip() or None
        status    = str(d.get("status","Present")).strip()
        reason    = str(d.get("reason","")).strip()

        emp = conn.execute("SELECT * FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
        if not emp: return jsonify({"success":False,"error":"Employee not found"})

        # Recalculate with new times
        save_att_row(conn, emp_code, att_date, in_time, out_time,
                    emp["category"], status=status)

        # Update remarks with edit reason
        existing_rem = conn.execute("SELECT remarks FROM attendance WHERE emp_code=? AND att_date=?",
            (emp_code, att_date)).fetchone()
        old_rem = (existing_rem["remarks"] or "") if existing_rem else ""
        new_rem = f"Edited: {reason}" if reason else "Manually edited"
        if old_rem and "Edited:" not in old_rem:
            new_rem = old_rem + " | " + new_rem

        conn.execute("""UPDATE attendance SET
            remarks=?, is_manual=1
            WHERE emp_code=? AND att_date=?""",
            (new_rem, emp_code, att_date))
        conn.commit()
        conn.close()
        return jsonify({"success":True, "message":f"Updated {emp_code} on {att_date}"})
    except Exception as e:
        conn.close()
        return jsonify({"success":False,"error":str(e)})

def cascade_recalculate_from_date(conn, emp_code, from_date_str):
    """
    eSSL-style position-based cascade recalculate.
    
    Build chronological flat punch stream from stored attendance:
    - For cross-midnight pairs (out < in): stored out belongs to NEXT day
    - Insert new manual punch at correct chronological position
    - Apply odd=IN, even=OUT position-based pairing
    - Save results back to attendance DB
    """
    from datetime import datetime as _drc, timedelta as _tdc, date as _dtcd
    from calendar import monthrange as _mrx
    try:
        from_dt = _dtcd.fromisoformat(from_date_str)
        year, month = from_dt.year, from_dt.month
        _, last_day = _mrx(year, month)
        to_date_str = f"{year}-{month:02d}-{last_day:02d}"

        # Also fetch 1 day before from_date to catch any cross-midnight punches
        fetch_from = (from_dt - _tdc(days=1)).strftime("%Y-%m-%d")

        rows = conn.execute("""SELECT att_date, in_time, out_time, status, remarks, is_manual
            FROM attendance
            WHERE emp_code=? AND att_date >= ? AND att_date <= ?
            ORDER BY att_date""",
            (emp_code, fetch_from, to_date_str)).fetchall()

        if not rows:
            return 0

        # ── Build correct chronological flat punch stream ──────────────
        # Manual entries are skipped from stream — they are directly in DB
        manual_dates_c = set()
        for row in rows:
            row_d = dict(row)
            if (row_d.get("is_manual") == 1 or
                (row_d.get("remarks") or "").startswith("Manual")):
                manual_dates_c.add(row_d["att_date"])

        # For each stored row:
        # - in_time → datetime on att_date
        # - out_time → if out < in (cross-midnight) → datetime on att_date+1
        #            → if out > in (same day) → datetime on att_date
        punch_dts = []  # list of datetime objects, sorted chronologically

        for row in rows:
            att_d = row["att_date"]
            # Skip manual entries from punch stream entirely
            if att_d in manual_dates_c:
                continue
            att_d = row["att_date"]
            in_t  = (row["in_time"]  or "").strip()[:5]
            out_t = (row["out_time"] or "").strip()[:5]

            if in_t:
                try:
                    punch_dts.append(_drc.strptime(f"{att_d} {in_t}", "%Y-%m-%d %H:%M"))
                except: pass

            if out_t:
                try:
                    out_dt = _drc.strptime(f"{att_d} {out_t}", "%Y-%m-%d %H:%M")
                    # Cross-midnight: out < in → belongs to next day chronologically
                    if in_t:
                        try:
                            in_dt = _drc.strptime(f"{att_d} {in_t}", "%Y-%m-%d %H:%M")
                            if out_dt < in_dt:
                                out_dt += _tdc(days=1)
                        except: pass
                    punch_dts.append(out_dt)
                except: pass

        # Also include punch_log entries (manual punches added by user)
        try:
            plog_rows = conn.execute("""SELECT punch_datetime, punch_type FROM punch_log
                WHERE emp_code=? AND punch_datetime >= ? AND punch_datetime <= ?
                ORDER BY punch_datetime""",
                (emp_code, f"{fetch_from} 00:00", f"{to_date_str} 23:59")).fetchall()
            for pl in plog_rows:
                try:
                    pl_dt = _drc.strptime(pl["punch_datetime"][:16], "%Y-%m-%d %H:%M")
                    punch_dts.append(pl_dt)
                except: pass
        except: pass

        if not punch_dts:
            return 0

        # Sort chronologically
        punch_dts = sorted(punch_dts)

        # Deduplicate within 2 minutes
        deduped = []
        for pt in punch_dts:
            if not deduped or (pt - deduped[-1]).total_seconds() > 600:  # 10-min dedup
                deduped.append(pt)
        punch_dts = deduped

        # ── eSSL Position-based pairing: odd=IN, even=OUT ─────────────
        # pos 1,3,5,7... = IN punch
        # pos 2,4,6,8... = OUT punch
        pairs = []
        i = 0
        while i < len(punch_dts):
            in_punch = punch_dts[i]
            i += 1
            out_punch = None
            if i < len(punch_dts):
                out_punch = punch_dts[i]
                i += 1

            att_date_str = in_punch.strftime("%Y-%m-%d")
            in_time_str  = in_punch.strftime("%H:%M")
            out_time_str = out_punch.strftime("%H:%M") if out_punch else None

            pairs.append({
                "date":     att_date_str,
                "in_time":  in_time_str,
                "out_time": out_time_str,
            })

        # ── Filter to from_date onwards only ──────────────────────────
        pairs = [p for p in pairs if p["date"] >= from_date_str]

        # ── Load holidays and weekly off ──────────────────────────────
        holiday_set = set()
        try:
            hols = conn.execute(
                "SELECT holiday_date FROM holidays WHERE holiday_date BETWEEN ? AND ?",
                (from_date_str, to_date_str)).fetchall()
            holiday_set = {h["holiday_date"] for h in hols}
        except: pass

        wo_num = get_emp_weekly_off_num(emp_code, conn)

        emp = conn.execute("SELECT category FROM employees WHERE emp_code=?",
                           (emp_code,)).fetchone()
        category = emp["category"] if emp else "Associate"

        # Roster map
        roster_map = {}
        try:
            rrows = conn.execute("""SELECT srd.shift_date, s.* FROM shifts s
                JOIN shift_roster_dates srd ON s.id=srd.shift_id
                WHERE srd.emp_code=? AND s.is_active=1""", (emp_code,)).fetchall()
            for r in rrows:
                roster_map[(emp_code, r["shift_date"])] = dict(r)
        except: pass

        # ── Save pairs to attendance DB ────────────────────────────────
        updated = 0
        for pair in pairs:
            att_d = pair["date"]
            if att_d < from_date_str or att_d > to_date_str:
                continue
            # Never overwrite manual entries
            if att_d in manual_dates_c:
                continue

            # Preserve Leave status AND manually-entered records
            existing = conn.execute(
                "SELECT status, remarks, is_manual FROM attendance WHERE emp_code=? AND att_date=?",
                (emp_code, att_d)).fetchone()
            if existing and existing["status"] == "Leave":
                continue
            if existing and existing["is_manual"] == 1:
                continue  # Manual entry — never overwrite

            try:
                att_dt_obj = _dtcd.fromisoformat(att_d)
                if att_dt_obj.weekday() == wo_num:
                    auto_status = "WOP"
                elif att_d in holiday_set:
                    auto_status = "Holiday"
                else:
                    auto_status = "Present"
            except:
                auto_status = "Present"

            pre_shift = roster_map.get((emp_code, att_d))
            old_remarks = (existing["remarks"] or "") if existing else ""

            save_att_row(conn, emp_code, att_d,
                        pair["in_time"], pair["out_time"],
                        category, status=auto_status, pre_shift=pre_shift)

            if "Manual" in old_remarks:
                conn.execute("UPDATE attendance SET remarks=? WHERE emp_code=? AND att_date=?",
                            (old_remarks, emp_code, att_d))
            updated += 1

        conn.commit()
        return updated

    except Exception as ex:
        import traceback; traceback.print_exc()
        return 0
    """
    eSSL-style cascade recalculate: after a manual punch is added on from_date,
    rebuild the entire punch stream from that date to end of month and re-pair,
    exactly like eSSL does when a missing punch is manually inserted.
    """
    from datetime import datetime as _drc, timedelta as _tdc, date as _dtcd
    from calendar import monthrange as _mrx
    try:
        from_dt = _dtcd.fromisoformat(from_date_str)
        year, month = from_dt.year, from_dt.month
        # End of month
        _, last_day = _mrx(year, month)
        to_date_str = f"{year}-{month:02d}-{last_day:02d}"

        # Fetch ALL attendance rows from from_date to end of month
        # Also fetch next day after month-end in case cross-midnight punch stored there
        from datetime import timedelta as _tdc2
        fetch_from = from_date_str
        # Also fetch 1 day before from_date to catch any punches stored on wrong date
        try:
            fetch_from_dt = _dtcd.fromisoformat(from_date_str) - _tdc2(days=1)
            fetch_from = fetch_from_dt.strftime("%Y-%m-%d")
        except: pass

        rows = conn.execute("""SELECT att_date, in_time, out_time, status, remarks, is_manual
            FROM attendance
            WHERE emp_code=? AND att_date >= ? AND att_date <= ?
            ORDER BY att_date""",
            (emp_code, fetch_from, to_date_str)).fetchall()

        if not rows:
            return 0

        # Build flat punch stream from stored attendance records
        # Key logic: if stored out_time < stored in_time → cross-midnight night shift
        # → stored out belongs to NEXT day (add +1 day to its datetime)
        # This correctly handles the "shifted data" pattern
        punch_dts = []
        for row in rows:
            att_d = row["att_date"]
            in_t  = (row["in_time"]  or "").strip()[:5]
            out_t = (row["out_time"] or "").strip()[:5]
            if in_t:
                try:
                    punch_dts.append(_drc.strptime(f"{att_d} {in_t}", "%Y-%m-%d %H:%M"))
                except: pass
            if out_t:
                try:
                    out_dt = _drc.strptime(f"{att_d} {out_t}", "%Y-%m-%d %H:%M")
                    # If stored out < stored in → cross-midnight → belongs to next day
                    if in_t:
                        try:
                            in_dt = _drc.strptime(f"{att_d} {in_t}", "%Y-%m-%d %H:%M")
                            if out_dt < in_dt:
                                out_dt += _tdc(days=1)  # place on next day
                        except: pass
                    punch_dts.append(out_dt)
                except: pass

        # Also include manual punch_log entries for this date range
        # These capture preserved machine punches that were overwritten during manual add
        try:
            plog_rows = conn.execute("""SELECT punch_datetime, punch_type FROM punch_log
                WHERE emp_code=? AND punch_datetime >= ? AND punch_datetime <= ?
                ORDER BY punch_datetime""",
                (emp_code, f"{fetch_from} 00:00", f"{to_date_str} 23:59")).fetchall()
            for pl in plog_rows:
                try:
                    pl_dt = _drc.strptime(pl["punch_datetime"][:16], "%Y-%m-%d %H:%M")
                    punch_dts.append(pl_dt)
                except: pass
        except: pass
        punch_dts = sorted(set(punch_dts))
        deduped = []
        for pt in punch_dts:
            if not deduped or (pt - deduped[-1]).total_seconds() > 600:  # 10-min dedup
                deduped.append(pt)
        punch_dts = deduped

        if not punch_dts:
            return 0

        # Get shifts for this employee
        emp = conn.execute("SELECT category, weekly_off FROM employees WHERE emp_code=?",
                           (emp_code,)).fetchone()
        category = emp["category"] if emp else "Associate"

        use_shifts = []
        try:
            grp_rows = conn.execute("""SELECT s.* FROM shifts s
                JOIN shift_group_members sgm ON s.id=sgm.shift_id
                JOIN employee_shift_groups esg ON sgm.group_id=esg.group_id
                WHERE esg.emp_code=? AND s.is_active=1""", (emp_code,)).fetchall()
            use_shifts = [dict(r) for r in grp_rows]
        except: pass
        if not use_shifts:
            use_shifts = get_all_shifts(conn)

        # Roster map for this employee
        roster_map = {}
        try:
            rrows = conn.execute("""SELECT srd.shift_date, s.* FROM shifts s
                JOIN shift_roster_dates srd ON s.id=srd.shift_id
                WHERE srd.emp_code=? AND s.is_active=1""", (emp_code,)).fetchall()
            for r in rrows:
                roster_map[(emp_code, r["shift_date"])] = dict(r)
        except: pass

        # Re-pair using eTimeTrack logic
        pairs = etimetrack_pair_punches(punch_dts, use_shifts, category=category,
                                        roster_map=roster_map)

        # Filter pairs to only from_date onwards — convert date objects to string safely
        def _to_str(d):
            return d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)

        pairs = [p for p in pairs if _to_str(p["date"]) >= from_date_str]

        # Save corrected pairs back to attendance
        updated = 0
        holiday_set = set()
        try:
            hols = conn.execute("SELECT holiday_date FROM holidays WHERE holiday_date BETWEEN ? AND ?",
                                (from_date_str, to_date_str)).fetchall()
            holiday_set = {h["holiday_date"] for h in hols}
        except: pass

        wo_num = get_emp_weekly_off_num(emp_code, conn)

        for pair in pairs:
            att_d = _to_str(pair["date"])
            if att_d < from_date_str or att_d > to_date_str:
                continue
            # Preserve Leave/Manual remarks
            existing = conn.execute(
                "SELECT status, remarks, is_manual FROM attendance WHERE emp_code=? AND att_date=?",
                (emp_code, att_d)).fetchone()
            if existing and existing["status"] == "Leave":
                continue  # Don't overwrite leave
            if existing and existing["is_manual"] == 1:
                continue  # Manual entry — protected from cascade overwrite

            try:
                att_dt_obj = _dtcd.fromisoformat(att_d)
                if att_dt_obj.weekday() == wo_num:
                    auto_status = "WOP"
                elif att_d in holiday_set:
                    auto_status = "Holiday"
                else:
                    auto_status = "Present"
            except:
                auto_status = "Present"

            pre_shift = roster_map.get((emp_code, att_d)) or pair.get("shift")
            old_remarks = (existing["remarks"] or "") if existing else ""
            save_att_row(conn, emp_code, att_d, pair["in_time"], pair["out_time"],
                        category, status=auto_status, pre_shift=pre_shift)
            # Preserve manual entry remark
            if "Manual" in old_remarks:
                conn.execute("UPDATE attendance SET remarks=? WHERE emp_code=? AND att_date=?",
                            (old_remarks, emp_code, att_d))
            updated += 1

        conn.commit()
        return updated
    except Exception as ex:
        import traceback; traceback.print_exc()
        return 0


@app.route("/attendance/reimport-employee", methods=["POST"])
@amgr
def reimport_employee_from_machine():
    """
    Fetch raw punches from biometric machine for a specific employee & month,
    then re-process attendance fresh. Used to fix wrong data after manual correction.
    """
    d         = request.json
    emp_code  = str(d.get("emp_code","")).strip()
    month     = int(d.get("month", date.today().month))
    year      = int(d.get("year",  date.today().year))
    machine_id= d.get("machine_id","")  # optional, if blank use all active machines

    if not emp_code:
        return jsonify({"success":False,"error":"Employee code required."})

    conn = get_db()
    try:
        emp = conn.execute("SELECT * FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
        if not emp:
            conn.close()
            return jsonify({"success":False,"error":f"Employee {emp_code} not found."})

        category = emp["category"]

        # Get machines to try
        if machine_id:
            machines = conn.execute("SELECT * FROM machines WHERE id=? AND is_active=1",
                                    (machine_id,)).fetchall()
        else:
            machines = conn.execute("SELECT * FROM machines WHERE is_active=1 ORDER BY id",
                                    ).fetchall()

        if not machines:
            conn.close()
            return jsonify({"success":False,
                "error":"No active biometric machines found. Please add machines in Biometric Machines page."})

        # Date range for this month
        from calendar import monthrange as _mr
        from datetime import datetime as _dtz, timedelta as _tdz
        from collections import defaultdict

        _, last_day = _mr(year, month)
        month_start = date(year, month, 1)
        month_end   = date(year, month, last_day)

        # Fetch raw punches from ALL machines
        all_punches = []  # list of datetime objects

        try:
            from zk import ZK
        except ImportError:
            conn.close()
            return jsonify({"success":False,
                "error":"ZK library not installed. Cannot connect to biometric machine."})

        machines_tried = 0
        for machine in machines:
            ip   = machine["ip_address"]
            port = machine["port"] or 4370
            pwd  = machine["password"] or 0
            try:
                zk  = ZK(ip, port=port, timeout=30, password=pwd,
                         force_udp=False, ommit_ping=True)
                czk = zk.connect()
                czk.disable_device()
                logs = czk.get_attendance()
                czk.enable_device()
                czk.disconnect()
                machines_tried += 1

                # Filter punches for this employee & month
                for log in logs:
                    uid = str(log.user_id).strip().lstrip("0") or "0"
                    # Match employee code variants
                    ec_clean = emp_code.strip().lstrip("0") or "0"
                    ec_full  = emp_code.strip()
                    if uid in [ec_clean, ec_full, emp_code.zfill(4), emp_code.zfill(3)]:
                        ts = log.timestamp
                        if isinstance(ts, _dtz):
                            if month_start <= ts.date() <= month_end:
                                all_punches.append(ts)
                        elif isinstance(ts, date):
                            if month_start <= ts <= month_end:
                                all_punches.append(_dtz.combine(ts, _dtz.min.time()))
            except Exception as mx:
                continue  # try next machine

        if machines_tried == 0:
            conn.close()
            return jsonify({"success":False,
                "error":"Could not connect to any biometric machine. Check machine is online and IP is correct."})

        if not all_punches:
            conn.close()
            return jsonify({"success":False,
                "error":f"No punch records found for employee {emp_code} in {MONTHS[month-1]} {year} on connected machines."})

        # Sort & deduplicate (within 2 min = same punch)
        all_punches = sorted(set(all_punches))
        deduped = []
        for pt in all_punches:
            if not deduped or (pt - deduped[-1]).total_seconds() > 600:  # 10-min dedup
                deduped.append(pt)
        all_punches = deduped

        # Get shifts
        use_shifts = []
        try:
            sg_rows = conn.execute("""SELECT s.* FROM shifts s
                JOIN shift_group_members sgm ON s.id=sgm.shift_id
                JOIN employee_shift_groups esg ON sgm.group_id=esg.group_id
                WHERE esg.emp_code=? AND s.is_active=1""", (emp_code,)).fetchall()
            use_shifts = [dict(r) for r in sg_rows]
        except: pass
        if not use_shifts:
            use_shifts = get_all_shifts(conn)

        # Roster map
        roster_map = {}
        try:
            rrows = conn.execute("""SELECT srd.shift_date, s.* FROM shifts s
                JOIN shift_roster_dates srd ON s.id=srd.shift_id
                WHERE srd.emp_code=? AND s.is_active=1""", (emp_code,)).fetchall()
            for r in rrows: roster_map[(emp_code, r["shift_date"])] = dict(r)
        except: pass

        # Holidays
        hol_rows = conn.execute(
            "SELECT holiday_date FROM holidays WHERE strftime('%m',holiday_date)=? AND strftime('%Y',holiday_date)=?",
            (f"{month:02d}", str(year))).fetchall()
        holiday_set = {h["holiday_date"] for h in hol_rows}

        wo_num = get_emp_weekly_off_num(emp_code, conn)

        # Re-pair using eTimeTrack logic
        pairs = etimetrack_pair_punches(all_punches, use_shifts, category=category,
                                        roster_map=roster_map)

        # Clear existing attendance for this employee & month (but preserve manual entries)
        manual_preserved = conn.execute("""SELECT att_date, in_time, out_time, status, remarks,
            working_minutes, late_minutes, short_minutes, ot_minutes, is_half_day, shift_name, late_waived
            FROM attendance WHERE emp_code=?
            AND strftime('%m',att_date)=? AND strftime('%Y',att_date)=?
            AND is_manual=1""",
            (emp_code, f"{month:02d}", str(year))).fetchall()
        conn.execute("""DELETE FROM attendance WHERE emp_code=?
            AND strftime('%m',att_date)=? AND strftime('%Y',att_date)=?
            AND (is_manual IS NULL OR is_manual=0)""",
            (emp_code, f"{month:02d}", str(year)))

        # Collect manually preserved dates — do not overwrite them
        manual_preserved_dates = {r["att_date"] for r in manual_preserved}

        # Save fresh pairs
        updated = 0
        for pair in pairs:
            att_d = _to_str(pair["date"]) if hasattr(pair["date"],"strftime") else str(pair["date"])
            if not (f"{year}-{month:02d}-01" <= att_d <= f"{year}-{month:02d}-{last_day:02d}"):
                continue
            # Never overwrite manually-entered attendance
            if att_d in manual_preserved_dates:
                continue
            try:
                att_dt_obj = date.fromisoformat(att_d)
                if att_dt_obj.weekday() == wo_num:
                    auto_status = "WOP"
                elif att_d in holiday_set:
                    auto_status = "Holiday"
                else:
                    auto_status = "Present"
            except:
                auto_status = "Present"

            pre_shift = roster_map.get((emp_code, att_d)) or pair.get("shift")
            save_att_row(conn, emp_code, att_d, pair["in_time"], pair["out_time"],
                        category, status=auto_status, pre_shift=pre_shift)
            updated += 1

        # Fill absent days (days with no pair)
        paired_dates = {_to_str(p["date"]) if hasattr(p["date"],"strftime") else str(p["date"])
                        for p in pairs}
        cur = month_start
        while cur <= month_end:
            cur_str = cur.strftime("%Y-%m-%d")
            # Never overwrite manually-entered records
            if cur_str in manual_preserved_dates:
                cur += _tdz(days=1)
                continue
            if cur_str not in paired_dates:
                # Check if WO or Holiday
                if cur.weekday() == wo_num:
                    save_att_row(conn, emp_code, cur_str, None, None, category, status="WO")
                elif cur_str in holiday_set:
                    save_att_row(conn, emp_code, cur_str, None, None, category, status="Holiday")
                else:
                    save_att_row(conn, emp_code, cur_str, None, None, category, status="Absent")
            cur += _tdz(days=1)

        conn.commit()
        conn.close()

        return jsonify({"success":True,
            "message":f"✅ Fresh re-import complete for {emp['emp_name']} — {MONTHS[month-1]} {year}. "
                      f"{len(all_punches)} raw punches processed, {updated} days updated.",
            "punches": len(all_punches), "updated": updated})

    except Exception as e:
        import traceback; traceback.print_exc()
        try: conn.close()
        except: pass
        return jsonify({"success":False,"error":str(e)})


@app.route("/attendance/punch-log")
@amgr
def get_punch_log():
    """Return recent manual punch log entries."""
    conn = get_db()
    rows = conn.execute("""
        SELECT pl.*, e.emp_name
        FROM punch_log pl
        LEFT JOIN employees e ON pl.emp_code=e.emp_code
        WHERE pl.added_on >= date('now','-30 days')
        ORDER BY pl.added_on DESC
        LIMIT 100""").fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/attendance/add-punch", methods=["POST"])
@amgr
def attendance_add_punch():
    """eSSL-style: add a single IN or OUT punch, save to punch_log, cascade recalculate."""
    d = request.json
    emp_code   = str(d.get("emp_code","")).strip()
    punch_date = str(d.get("punch_date","")).strip()
    punch_time = str(d.get("punch_time","")).strip()
    punch_type = str(d.get("punch_type","IN")).strip().upper()  # IN or OUT
    remarks    = str(d.get("remarks","")).strip()
    extra_out  = str(d.get("out_time","") or "").strip()  # optional OUT with IN

    if not emp_code or not punch_date or not punch_time:
        return jsonify({"success":False,"error":"Employee, date and time are required."})

    conn = get_db()
    try:
        emp = conn.execute("SELECT * FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
        if not emp:
            conn.close()
            return jsonify({"success":False,"error":f"Employee {emp_code} not found."})

        punch_datetime_str = f"{punch_date} {punch_time}"

        # Save to punch_log
        conn.execute("""INSERT OR REPLACE INTO punch_log
            (emp_code, punch_datetime, punch_type, source, added_by, added_on, remarks)
            VALUES (?,?,?,'Manual',?,?,?)""",
            (emp_code, punch_datetime_str, punch_type,
             session.get("username","Admin"),
             datetime.now().strftime("%Y-%m-%d %H:%M"), remarks))

        # Get existing attendance record for this date
        existing = conn.execute(
            "SELECT in_time, out_time FROM attendance WHERE emp_code=? AND att_date=?",
            (emp_code, punch_date)).fetchone()

        curr_in  = existing["in_time"]  if existing else None
        curr_out = existing["out_time"] if existing else None

        # CRITICAL: Save existing times to punch_log before overwriting
        # so cascade can use them as raw punches in the correct position
        if punch_type == "IN" and curr_in and curr_in.strip():
            # Old in_time (e.g. 19:27) was stored on this date but may be evening OUT
            # Save it to punch_log as an OUT punch so cascade includes it
            conn.execute("""INSERT OR IGNORE INTO punch_log
                (emp_code, punch_datetime, punch_type, source, added_by, added_on, remarks)
                VALUES (?,?,?,'Machine-Preserved',?,?,?)""",
                (emp_code, f"{punch_date} {curr_in.strip()}", "OUT",
                 session.get("username","Admin"),
                 datetime.now().strftime("%Y-%m-%d %H:%M"),
                 "Auto-preserved existing in_time as OUT during manual IN add"))
        if punch_type == "IN" and curr_out and curr_out.strip():
            # Old out_time (e.g. 08:25 cross-midnight) → save as next day IN to punch_log
            from datetime import date as _dpd, timedelta as _tdpd
            try:
                next_date = (_dpd.fromisoformat(punch_date) + _tdpd(days=1)).strftime("%Y-%m-%d")
                _in_m  = int(punch_time[:2])*60 + int(punch_time[3:5])
                _out_m = int(curr_out.strip()[:2])*60 + int(curr_out.strip()[3:5])
                if _out_m < _in_m:  # cross-midnight orphan → belongs to next day
                    conn.execute("""INSERT OR IGNORE INTO punch_log
                        (emp_code, punch_datetime, punch_type, source, added_by, added_on, remarks)
                        VALUES (?,?,?,'Machine-Preserved',?,?,?)""",
                        (emp_code, f"{next_date} {curr_out.strip()}", "IN",
                         session.get("username","Admin"),
                         datetime.now().strftime("%Y-%m-%d %H:%M"),
                         "Auto-preserved cross-midnight out as next day IN"))
            except: pass

        # Apply punch to attendance record
        if punch_type == "IN":
            new_in = punch_time
            # If user provided optional OUT time → use it directly (guaranteed correct)
            if extra_out:
                new_out = extra_out
            elif curr_out:
                try:
                    _in_m  = int(punch_time[:2])*60 + int(punch_time[3:5])
                    _out_m = int(curr_out[:2])*60   + int(curr_out[3:5])
                    new_out = curr_out if _out_m > _in_m else None
                except:
                    new_out = curr_out
            else:
                new_out = None
        else:  # OUT
            new_in  = curr_in
            new_out = punch_time

        # Save/update attendance for this date
        # Check if this date is employee's weekly off day → WOP, not Present
        from datetime import date as _wpd
        try:
            _d_obj = _wpd.fromisoformat(punch_date)
            _is_wo = (_d_obj.weekday() == get_emp_weekly_off_num(emp_code, conn))
        except:
            _is_wo = False
        _punch_status = "WOP" if _is_wo else "Present"
        save_att_row(conn, emp_code, punch_date, new_in, new_out,
                     emp["category"], status=_punch_status,
                     status_override="WOP" if _is_wo else None)

        # Mark as manual + update remarks — protects from cascade overwrite
        conn.execute("""UPDATE attendance SET
            is_manual=1,
            remarks=?
            WHERE emp_code=? AND att_date=?""",
            (f"Manual punch ({punch_type}): {remarks}" if remarks else f"Manual {punch_type} punch",
             emp_code, punch_date))
        conn.commit()
        conn.close()
        msg = f"{punch_type} punch added for {emp['emp_name']} on {punch_date} at {punch_time}."
        return jsonify({"success":True,"message":msg})

    except Exception as e:
        try: conn.close()
        except: pass
        return jsonify({"success":False,"error":str(e)})


@app.route("/attendance/manual-add", methods=["GET","POST"])
@amgr
def attendance_manual_add():
    """Add attendance manually for field/outside work employees"""
    if request.method == "POST":
        d = request.json
        conn = get_db()
        try:
            emp_code  = str(d.get("emp_code","")).strip()
            att_date  = str(d.get("att_date","")).strip()
            in_time   = str(d.get("in_time","")).strip() or None
            out_time  = str(d.get("out_time","")).strip() or None
            remarks   = str(d.get("remarks","")).strip()
            status    = str(d.get("status","Present")).strip()

            emp = conn.execute("SELECT * FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
            if not emp:
                return jsonify({"success":False,"error":f"Employee {emp_code} not found"})

            # Preserve existing in/out if only one side is being manually entered
            existing = conn.execute(
                "SELECT in_time, out_time FROM attendance WHERE emp_code=? AND att_date=?",
                (emp_code, att_date)).fetchone()
            if existing:
                if not in_time and existing["in_time"]:
                    in_time = existing["in_time"]   # keep existing IN
                if not out_time and existing["out_time"]:
                    out_time = existing["out_time"] # keep existing OUT

            # Check if this date is employee's weekly off → override to WOP
            from datetime import date as _wmd
            try:
                _dm = _wmd.fromisoformat(att_date)
                if _dm.weekday() == get_emp_weekly_off_num(emp_code, conn):
                    status = "WOP"
            except:
                pass
            # Save attendance
            save_att_row(conn, emp_code, att_date, in_time, out_time,
                        emp["category"], status=status,
                        status_override="WOP" if status=="WOP" else None)

            # Mark as manual entry — cascade will not overwrite this
            conn.execute("""UPDATE attendance SET
                is_manual=1,
                remarks=?
                WHERE emp_code=? AND att_date=?""",
                (f"Manual entry: {remarks}" if remarks else "Manual entry",
                 emp_code, att_date))
            conn.commit()
            conn.close()
            msg = f"Attendance updated for {emp['emp_name']} on {att_date}."
            return jsonify({"success":True, "message": msg})
        except Exception as e:
            conn.close()
            return jsonify({"success":False,"error":str(e)})

    # GET: render form
    conn = get_db()
    emps = conn.execute("SELECT emp_code,emp_name,department,category FROM employees WHERE status='Active' ORDER BY emp_name").fetchall()
    depts = conn.execute("SELECT DISTINCT department FROM employees WHERE status='Active' ORDER BY department").fetchall()
    machines = conn.execute("SELECT id,machine_name,ip_address FROM machines WHERE is_active=1 ORDER BY machine_name").fetchall()
    conn.close()
    return render_template("attendance_manual.html",
        employees=[dict(e) for e in emps],
        departments=[d["department"] for d in depts],
        machines=[dict(m) for m in machines],
        today=date.today().strftime("%Y-%m-%d"),
        today_month=date.today().month,
        today_year=date.today().year,
        month_names=MONTHS)

@app.route("/attendance/manual-bulk", methods=["POST"])
@amgr
def attendance_manual_bulk():
    """Add multiple manual attendance records at once"""
    d = request.json
    conn = get_db()
    added = 0; errors = []
    try:
        for rec in d.get("records",[]):
            emp_code = str(rec.get("emp_code","")).strip()
            att_date = str(rec.get("att_date","")).strip()
            in_time  = str(rec.get("in_time","")).strip() or None
            out_time = str(rec.get("out_time","")).strip() or None
            remarks  = rec.get("remarks","Outside/Field work")
            status   = rec.get("status","Present")

            emp = conn.execute("SELECT * FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
            if not emp:
                errors.append(f"{emp_code}: not found"); continue

            save_att_row(conn, emp_code, att_date, in_time, out_time,
                        emp["category"], status=status)
            conn.execute("""UPDATE attendance SET
                remarks=?, is_manual=1
                WHERE emp_code=? AND att_date=?""",
                (f"Manual: {remarks}", emp_code, att_date))
            added += 1
        conn.commit()
        return jsonify({"success":True,"added":added,"errors":errors[:5]})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})
    finally: conn.close()

@app.route("/attendance")
@amgr
def attendance():
    month=int(request.args.get("month",date.today().month))
    year =int(request.args.get("year", date.today().year))
    dept =request.args.get("dept","")
    selected_date=request.args.get("sel_date","")  # Date filter from URL
    conn=get_db()
    show_inactive = request.args.get("show_inactive","0") == "1"
    # Get allowed departments for this user
    allowed_depts = get_user_depts()
    if show_inactive:
        q = "SELECT emp_code,emp_name,category,department,status FROM employees WHERE status='Inactive'"
    else:
        q = "SELECT emp_code,emp_name,category,department,status FROM employees WHERE status='Active'"
    if dept:
        q += " AND department=?"
    elif allowed_depts:
        # Restrict to user's allowed departments
        placeholders = ",".join(["?"]*len(allowed_depts))
        q += f" AND department IN ({placeholders})"
    q += " ORDER BY department,category,emp_name"
    if dept:
        emps = conn.execute(q, (dept,)).fetchall()
    elif allowed_depts:
        emps = conn.execute(q, tuple(allowed_depts)).fetchall()
    else:
        emps = conn.execute(q).fetchall()
    depts=conn.execute("SELECT DISTINCT department FROM employees WHERE status='Active' ORDER BY department").fetchall()
    att_data={}
    latest_att={}
    # Single bulk query instead of N queries
    all_month_att = conn.execute("""SELECT * FROM attendance
        WHERE strftime('%m',att_date)=? AND strftime('%Y',att_date)=?
        ORDER BY emp_code, att_date""",
        (f"{month:02d}", str(year))).fetchall()
    from collections import defaultdict
    _att_grouped = defaultdict(list)
    for r in all_month_att:
        # Normalize emp_code: try both raw and stripped-zero variants
        ec_raw = str(r["emp_code"]).strip()
        _att_grouped[ec_raw].append(r)
        # Also index by stripped zeros so "0037" matches "37"
        ec_stripped = ec_raw.lstrip("0") or "0"
        if ec_stripped != ec_raw:
            _att_grouped[ec_stripped].append(r)
    for e in emps:
        ec = str(e["emp_code"]).strip()
        # Try exact match first, then stripped
        rows = _att_grouped.get(ec) or _att_grouped.get(ec.lstrip("0") or "0") or []
        att_data[e["emp_code"]] = {r["att_date"]: dict(r) for r in rows}
        if rows:
            sel_rec = att_data[e["emp_code"]].get(selected_date)
            if sel_rec:
                latest_att[e["emp_code"]] = sel_rec
            else:
                latest = max(rows, key=lambda r: r["att_date"])
                latest_att[e["emp_code"]] = dict(latest)
    today_str=date.today().strftime("%Y-%m-%d")
    machines = conn.execute("SELECT * FROM machines WHERE is_active=1 ORDER BY machine_name").fetchall()
    machines_list = [dict(m) for m in machines]
    # Convert to dicts for JSON serialization in template - include ALL fields used in template
    emps_list = [{"emp_code":e["emp_code"],"emp_name":e["emp_name"],
                  "department":e["department"] or "","category":e["category"] or ""} for e in emps]
    conn.close()
    return render_template("attendance.html",employees=emps_list,att_data=att_data,
        latest_att=latest_att,
        month=month,year=year,months=MONTHS,
        working_days_staff=get_wd(year,month,"Staff"),
        month_name=MONTHS[month-1],today_str=today_str,
        today_m=date.today().month, today_y=date.today().year,
        selected_date=selected_date,
        departments=[d[0] for d in depts],selected_dept=dept,
        machines=machines_list,
        ghost_codes=[])

@app.route("/attendance/save",methods=["POST"])
@amgr
def save_att():
    data=request.json; conn=get_db()
    try:
        emp=conn.execute("SELECT category FROM employees WHERE emp_code=?",(data["emp_code"],)).fetchone()
        if not emp: return jsonify({"success":False,"error":"Not found"})
        # Use att_date from request (can be any date, not just today)
        att_date = data.get("att_date", date.today().strftime("%Y-%m-%d"))
        c=save_att_row(conn,data["emp_code"],att_date,
                      data.get("in_time",""),data.get("out_time",""),
                      emp["category"],data.get("status","Present"))
        conn.commit(); return jsonify({"success":True,"calc":c,"att_date":att_date})
    except Exception as e: return jsonify({"success":False,"error":str(e)})
    finally: conn.close()

@app.route("/attendance/get-record",methods=["POST"])
@amgr
def get_att_record():
    """Get attendance record for specific employee and date"""
    data=request.json; conn=get_db()
    emp_code = data.get("emp_code"); att_date = data.get("att_date")
    row = conn.execute("SELECT * FROM attendance WHERE emp_code=? AND att_date=?",
                      (emp_code, att_date)).fetchone()
    conn.close()
    if row: return jsonify({"found":True, "record":dict(row)})
    return jsonify({"found":False, "record":{}})


def _run_zk_import(month, year, all_months, ip, port, password):
    """Background ZK attendance import — runs in thread so HTTP never times out."""
    global import_progress
    try:
        from zk import ZK
        from collections import defaultdict
    
        import_progress.update({"stage":"Connecting to machine...","percent":10})
        zk  = ZK(ip,port=port,timeout=60,password=password,force_udp=False,ommit_ping=True)
        czk = zk.connect(); czk.disable_device()
    
        import_progress.update({"stage":"Downloading logs from machine...","percent":20})
        logs= czk.get_attendance(); czk.enable_device(); czk.disconnect()
    
        total_logs = len(logs)
        import_progress.update({"stage":f"Downloaded {total_logs:,} logs. Filtering...","total":total_logs,"percent":35})
    
        # ── eTimeTrack style: collect ALL punches per employee chronologically ──
        from datetime import datetime as _dtt
        import calendar as _cal_zk
        from datetime import date as _dtb_zk, timedelta as _td_zk
        # Collect ALL punches for each employee — full chronological history
        emp_punches = defaultdict(list)  # uid -> [datetime, ...]
        for log in logs:
            emp_punches[str(log.user_id)].append(log.timestamp)
    
        total_sessions = len(emp_punches)
    
        conn = get_db()
        all_shifts = get_all_shifts(conn)
    
        # ── BULK PRELOAD — avoids N+1 queries inside loop ──────────────────
        # 1. Preload ALL employees (active + inactive) for fast ID matching
        all_emps_rows = conn.execute("SELECT emp_code, category, status FROM employees").fetchall()
        emp_map = {}  # normalised_code → emp_row
        for e in all_emps_rows:
            ec_raw = str(e["emp_code"]).strip()
            for variant in [ec_raw, ec_raw.zfill(4), ec_raw.zfill(3),
                            ec_raw.lstrip("0") or "0",
                            str(int(ec_raw)) if ec_raw.isdigit() else ec_raw]:
                if variant not in emp_map:
                    emp_map[variant] = e
    
        # 2. Preload ALL shift group assignments
        try:
            sg_rows = conn.execute("""SELECT esg.emp_code, s.*
                FROM shifts s
                JOIN shift_group_members sgm ON s.id=sgm.shift_id
                JOIN employee_shift_groups esg ON sgm.group_id=esg.group_id
                WHERE s.is_active=1""").fetchall()
            shift_by_emp = {}
            for r in sg_rows:
                shift_by_emp.setdefault(r["emp_code"], []).append(dict(r))
        except:
            shift_by_emp = {}
    
        # 3. Preload ALL holidays for this month (or all months)
        if all_months:
            hol_rows = conn.execute("SELECT holiday_date, applies_to FROM holidays").fetchall()
        else:
            hol_rows = conn.execute(
                "SELECT holiday_date, applies_to FROM holidays WHERE strftime('%m',holiday_date)=? AND strftime('%Y',holiday_date)=?",
                (f"{month:02d}", str(year))).fetchall()
        holiday_set = {r["holiday_date"] for r in hol_rows}
    
        # 4. Preload roster overrides
        try:
            roster_rows = conn.execute("""SELECT srd.emp_code, srd.shift_date, s.*
                FROM shifts s JOIN shift_roster_dates srd ON s.id=srd.shift_id
                WHERE s.is_active=1""").fetchall()
            roster_map = {}
            for r in roster_rows:
                roster_map[(r["emp_code"], r["shift_date"])] = dict(r)
        except:
            roster_map = {}
    
        # 5. Preload late counts per employee per month (avoids per-row SELECT in save_att_row)
        try:
            late_rows = conn.execute("""
                SELECT emp_code,
                       CAST(strftime('%m',att_date) AS INTEGER) as mn,
                       CAST(strftime('%Y',att_date) AS INTEGER) as yr,
                       COUNT(*) as cnt
                FROM attendance WHERE late_minutes>0
                GROUP BY emp_code, mn, yr
            """).fetchall()
            # dict: (emp_code, month, year) → count
            late_count_map = {(r["emp_code"], r["mn"], r["yr"]): r["cnt"] for r in late_rows}
        except:
            late_count_map = {}

        # 6. Preload all manually-entered attendance records — these must NEVER be overwritten
        #    by any machine import, auto-import, or bulk recalculate.
        try:
            manual_rows = conn.execute(
                "SELECT emp_code, att_date FROM attendance WHERE is_manual=1"
            ).fetchall()
            manual_att_set = {(r["emp_code"], r["att_date"]) for r in manual_rows}
        except:
            manual_att_set = set()

        import_progress.update({"stage": "Data preloaded. Processing punches...", "percent": 45})
        # ───────────────────────────────────────────────────────────────────
    
        imported=0; batch=0; processed=0
        unmatched_ids   = set()
        duplicate_count = 0
        no_out_count    = 0
        error_records   = []
        bulk_rows       = []  # collect all rows for bulk insert
    
        for uid_raw, punches in emp_punches.items():
            try:
                # Fast lookup from preloaded map
                uid_str = str(uid_raw).strip()
                emp = None
                matched_uid = None
                for variant in [uid_str, uid_str.zfill(4), uid_str.zfill(3),
                                 uid_str.lstrip("0") or "0",
                                 str(int(uid_str)) if uid_str.isdigit() else uid_str]:
                    if variant in emp_map:
                        emp = emp_map[variant]; matched_uid = emp["emp_code"]; break
                if not emp:
                    unmatched_ids.add(uid_raw)
                    continue
                ec       = matched_uid
                category = emp["category"]
    
                # Shifts from preloaded map
                grp_shifts = shift_by_emp.get(ec, [])
                use_shifts = grp_shifts if grp_shifts else all_shifts
    
                # Sort punches chronologically
                punches_sorted = sorted(set(punches))

                # Build per-employee roster_map for window pairing
                emp_roster_map = {k: v for k, v in roster_map.items()
                                  if isinstance(k, tuple) and k[0] == ec}

                # eTimeTrack pair — Shift Window Based
                pairs = etimetrack_pair_punches(punches_sorted, use_shifts,
                                                category=category,
                                                roster_map=emp_roster_map)
    
                for pair in pairs:
                    att_date = pair["date"]
                    # Only save records for requested month (or all_months)
                    if not all_months:
                        try:
                            import datetime as _dtimp
                            ad = _dtimp.date.fromisoformat(att_date)
                            if ad.month != month or ad.year != year:
                                continue
                        except: pass
    
                    pi = pair["in_time"]
                    po = pair["out_time"]
                    if not po: no_out_count += 1
    
                    # Resolve shift strictly from Roster only
                    pre_shift = roster_map.get((ec, att_date))  # Roster → fixed via save_att_row; NO auto-detect
    
                    try:
                        from datetime import date as _dtc
                        att_dt = _dtc.fromisoformat(att_date)
                        if att_dt.weekday() == get_emp_weekly_off_num(ec, conn):
                            auto_status = "WOP"
                        else:
                            auto_status = "Holiday" if att_date in holiday_set else "Present"
                    except:
                        auto_status = "Present"
    
                    # ── Miss Punch: IN present, OUT missing ─────────────────────────
                    # Past dates → always Miss Punch (shift ended long ago)
                    # Today → only Miss Punch after shift end + 30 min grace
                    if pi and not po and auto_status not in ("WOP", "Holiday"):
                        _is_miss = False
                        try:
                            from datetime import datetime as _dtnow_mp, date as _date_mp
                            _att_d = _date_mp.fromisoformat(att_date)
                            _today = _date_mp.today()
                            if _att_d < _today:
                                # Past date — shift definitely ended → Miss Punch
                                _is_miss = True
                            else:
                                # Today — check if shift end + 30 min passed
                                _now_m = _dtnow_mp.now().hour * 60 + _dtnow_mp.now().minute
                                _sh_row = roster_map.get((ec, att_date))
                                _end_t = _sh_row.get("end_time","") if _sh_row else ""
                                if _end_t:
                                    _eh, _em = map(int, _end_t.split(":")[:2])
                                    _end_m = _eh * 60 + _em
                                    _is_night = _sh_row.get("is_night_shift",0) if _sh_row else 0
                                    if _is_night and _end_m < 12*60:
                                        _end_m += 24*60
                                    if _now_m > _end_m + 30:
                                        _is_miss = True
                                else:
                                    # No shift — 9 hrs after IN
                                    _in_h, _in_m2 = map(int, str(pi).split(":")[:2])
                                    if _now_m > _in_h*60 + _in_m2 + 9*60:
                                        _is_miss = True
                        except:
                            _is_miss = True
                        if _is_miss:
                            auto_status = "Miss Punch"
    
                    # MANUAL ENTRY PROTECTION: skip dates that were manually edited
                    if (ec, att_date) in manual_att_set:
                        continue  # never overwrite manual entries during machine import

                    res = save_att_row(None, ec, att_date, pi, po, category,
                                 status=auto_status, pre_shift=pre_shift,
                                 late_count_map=late_count_map)
                    if res and "_row_params" in res:
                        bulk_rows.append(res["_row_params"])
                    imported += 1; batch += 1
    
                processed += 1
                batch += 1
                # Update progress every 200 employees (not every 50) — no commit inside loop
                if batch >= 200:
                    batch = 0
                    pct = 50 + int((processed / max(total_sessions,1)) * 45)
                    import_progress.update({
                        "stage": f"Processing records... {processed:,} / {total_sessions:,}",
                        "processed": processed,
                        "imported":  imported,
                        "skipped":   len(unmatched_ids),
                        "percent":   min(pct, 95)
                    })
    
            except Exception as rec_err:
                error_records.append(f"{uid_raw}: {str(rec_err)[:80]}")
    
        # ── BULK INSERT all collected rows ─────────────────────────────────
        if bulk_rows:
            import_progress.update({"stage": f"Saving {len(bulk_rows):,} records to database...", "percent": 96})
            conn.executemany("""INSERT INTO attendance
                (emp_code,att_date,in_time,out_time,working_minutes,status,
                 late_minutes,short_minutes,ot_minutes,is_half_day,shift_name)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(emp_code,att_date) DO UPDATE SET
                in_time=excluded.in_time,
                out_time=excluded.out_time,
                working_minutes=excluded.working_minutes,
                status=excluded.status,
                late_minutes=excluded.late_minutes,
                short_minutes=excluded.short_minutes,
                ot_minutes=excluded.ot_minutes,
                is_half_day=excluded.is_half_day,
                shift_name=excluded.shift_name
                WHERE (attendance.is_manual IS NULL OR attendance.is_manual = 0)""", bulk_rows)
    
        conn.commit(); conn.close()
    
        # Final status update — visible on frontend via /attendance/import-progress
        import_progress.update({
            "status":                "done",
            "stage":                 f"✅ Import complete — {imported:,} records saved.",
            "percent":               100,
            "success":               True,
            "imported":              imported,
            "total":                 len(logs),
            "total_unique_sessions": len(emp_punches),
            "duplicates_removed":    duplicate_count,
            "no_out_punch":          no_out_count,
            "unmatched_count":       len(unmatched_ids),
            "unmatched_ids":         sorted(list(unmatched_ids))[:20],
            "error_count":           len(error_records),
            "errors":                error_records[:5],
        })
    except ImportError:
        import_progress.update({"status":"error","error":"Run: pip install pyzk","percent":0})
    except Exception as e:
        import_progress.update({"status":"error","error":str(e),"percent":0})
    

@app.route("/attendance/import-machine",methods=["POST"])
@amgr
def import_machine():
    global import_progress
    data      = request.json
    month     = int(data.get("month",date.today().month))
    year      = int(data.get("year", date.today().year))
    all_months= data.get("all_months", False)
    ip        = data.get("ip","192.168.0.125")
    port      = int(data.get("port",4370))
    password  = int(data.get("password",0))

    # Reset progress
    import_progress.update({"status":"running","stage":"Connecting to machine...","total":0,"processed":0,"imported":0,"skipped":0,"percent":5,"message":"","error":""})

    # Start in background thread — returns immediately, no HTTP timeout
    import threading as _thr
    t = _thr.Thread(
        target=_run_zk_import,
        args=(month, year, all_months, ip, port, password),
        daemon=True
    )
    t.start()
    return jsonify({"success": True, "started": True, "message": "Import started"})

@app.route("/attendance/import-progress")
@amgr
def get_import_progress():
    """Live progress polling endpoint"""
    return jsonify(import_progress)

@app.route("/attendance/machine-info",methods=["POST"])
@amgr
def machine_info():
    data=request.json
    ip=data.get("ip","192.168.0.125"); port=int(data.get("port",4370)); password=int(data.get("password",0))
    try:
        from zk import ZK
        zk=ZK(ip,port=port,timeout=60,password=password,force_udp=False,ommit_ping=True)
        czk=zk.connect(); logs=czk.get_attendance(); czk.disconnect()
        if not logs: return jsonify({"success":True,"total":0,"first":"No logs","last":"No logs","months":{}})
        timestamps=[l.timestamp for l in logs]
        from collections import Counter
        month_counts=Counter(l.timestamp.strftime("%B %Y") for l in logs)
        return jsonify({"success":True,"total":len(logs),
            "first":min(timestamps).strftime("%d %B %Y"),
            "last":max(timestamps).strftime("%d %B %Y"),
            "months":dict(month_counts)})
    except ImportError: return jsonify({"success":False,"error":"Run: pip install pyzk"})
    except Exception as e: return jsonify({"success":False,"error":str(e)})

@app.route("/attendance/connect-machine",methods=["POST"])
@amgr
def connect_machine():
    data=request.json
    try:
        from zk import ZK
        zk=ZK(data.get("ip","192.168.0.125"),port=int(data.get("port",4370)),timeout=60,
              password=int(data.get("password",0)),force_udp=False,ommit_ping=True)
        czk=zk.connect()
        name=czk.get_device_name() or "eSSL UFace302"
        czk.disconnect()
        return jsonify({"success":True,"device":name})
    except ImportError: return jsonify({"success":False,"error":"Run: pip install pyzk"})
    except Exception as e: return jsonify({"success":False,"error":str(e)})

@app.route("/attendance/test-db-connection", methods=["POST"])
@amgr
def test_db_connection():
    """MS SQL Server connection test — import se pehle verify karo"""
    data = request.json
    server   = data.get("host", "VPLDCSRV\\INDUS")
    database = data.get("database", "etimetracklite1")
    uid_sql  = data.get("user", "")
    pwd_sql  = data.get("password", "")
    try:
        import pyodbc
        # Try multiple connection string formats
        errors = []
        connection_strings = []

        if uid_sql and pwd_sql:
            # SQL Server Authentication
            connection_strings = [
                f"DRIVER={{SQL Server}};SERVER={server};DATABASE={database};UID={uid_sql};PWD={pwd_sql};TrustServerCertificate=yes;",
                f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={server};DATABASE={database};UID={uid_sql};PWD={pwd_sql};TrustServerCertificate=yes;",
                f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={server};DATABASE={database};UID={uid_sql};PWD={pwd_sql};TrustServerCertificate=yes;Encrypt=no;",
            ]
        else:
            # Windows Authentication
            connection_strings = [
                f"DRIVER={{SQL Server}};SERVER={server};DATABASE={database};Trusted_Connection=yes;TrustServerCertificate=yes;",
                f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={server};DATABASE={database};Trusted_Connection=yes;TrustServerCertificate=yes;",
                f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={server};DATABASE={database};Trusted_Connection=yes;TrustServerCertificate=yes;Encrypt=no;",
            ]

        connected_cs = None
        for cs in connection_strings:
            try:
                bio = pyodbc.connect(cs, timeout=8)
                cur = bio.cursor()
                # Check if Atten table exists
                cur.execute("SELECT COUNT(*) FROM dbo.Atten")
                count = cur.fetchone()[0]
                bio.close()
                connected_cs = cs.split(";")[0]  # just driver name
                return jsonify({
                    "success": True,
                    "message": f"✅ Connected! {count:,} records found in dbo.Atten table.",
                    "driver": connected_cs,
                    "record_count": count
                })
            except Exception as e:
                errors.append(str(e)[:100])
                continue

        return jsonify({
            "success": False,
            "error": "No database driver could connect. Please check your connection settings.",
            "tried": len(connection_strings),
            "errors": errors
        })

    except ImportError:
        return jsonify({"success": False, "error": "pyodbc is not installed. Run: pip install pyodbc"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/attendance/import-database",methods=["POST"])
@amgr
def import_database():
    """
    eTimeTrack MS SQL Server — dbo.Atten table se import
    ═══════════════════════════════════════════════════════
    Table: etimetracklite1.dbo.Atten
    Columns used:
      Empcode     → employee code
      EntryDate   → date of punch
      EntryTime   → actual punch time (datetime)
      InOutFlag   → '0' or 'in'  = IN punch
                    '1' or 'out' = OUT punch
                    NULL / other = treat as both (use for IN+OUT logic)

    Logic:
      - InOutFlag se clearly IN aur OUT alag karo
      - Ek din ka pehla IN punch = IN time
      - Ek din ka aakhri OUT punch = OUT time
      - Agar sirf IN punches hain (no OUT flag) = IN only
      - Agar sirf OUT punches hain (no IN flag) = OUT only
      - Next-day OUT merge bhi handle hoga
    """
    data  = request.json
    month = int(data.get("month", date.today().month))
    year  = int(data.get("year",  date.today().year))
    all_months = data.get("all_months", False)

    try:
        import pyodbc
        from collections import defaultdict

        # ── Connection — Multiple drivers try karo ──────────
        server   = data.get("host",     "VPLDCSRV\\INDUS")
        database = data.get("database", "etimetracklite1")
        uid_sql  = data.get("user",     "")
        pwd_sql  = data.get("password", "")

        def make_cs_list(srv, db, uid, pwd):
            """Multiple connection strings — different drivers try karo"""
            if uid and pwd:
                auth = f"UID={uid};PWD={pwd};"
            else:
                auth = "Trusted_Connection=yes;"
            return [
                f"DRIVER={{SQL Server}};SERVER={srv};DATABASE={db};{auth}TrustServerCertificate=yes;",
                f"DRIVER={{ODBC Driver 17 for SQL Server}};SERVER={srv};DATABASE={db};{auth}TrustServerCertificate=yes;",
                f"DRIVER={{ODBC Driver 18 for SQL Server}};SERVER={srv};DATABASE={db};{auth}TrustServerCertificate=yes;Encrypt=no;",
                f"DRIVER={{SQL Server Native Client 11.0}};SERVER={srv};DATABASE={db};{auth}TrustServerCertificate=yes;",
            ]

        bio = None
        last_error = ""
        for cs in make_cs_list(server, database, uid_sql, pwd_sql):
            try:
                bio = pyodbc.connect(cs, timeout=15)
                break
            except Exception as ce:
                last_error = str(ce)
                continue

        if bio is None:
            return jsonify({"success": False,
                            "error": f"Connection failed! {last_error}\n\nTip: Leave Username/Password blank for Windows Authentication"})
        cur = bio.cursor()

        # ── Fetch from dbo.Atten ──────────────────────────
        # InOutFlag: '0'=IN, '1'=OUT (eTimeTrack standard)
        if all_months:
            cur.execute("""
                SELECT Empcode, CAST(EntryDate AS DATE) as att_date,
                       CONVERT(varchar(5), EntryTime, 108) as punch_time,
                       InOutFlag
                FROM dbo.Atten
                WHERE EntryDate IS NOT NULL AND Empcode IS NOT NULL
                ORDER BY Empcode, EntryDate, EntryTime
            """)
        else:
            # Fetch all punches — need complete history for correct pairing
            cur.execute("""
                SELECT Empcode, CAST(EntryDate AS DATE) as att_date,
                       CONVERT(varchar(5), EntryTime, 108) as punch_time,
                       InOutFlag
                FROM dbo.Atten
                WHERE EntryDate IS NOT NULL AND Empcode IS NOT NULL
                ORDER BY Empcode, EntryDate, EntryTime
            """)

        rows = cur.fetchall()
        bio.close()

        # ── eTimeTrack style: group by empcode, sort chronologically ──
        from datetime import datetime as _dtt
        emp_punches_sql = defaultdict(list)  # empcode -> [(datetime, flag), ...]

        def _tm(s):
            try: h,m2=map(int,str(s).strip().split(":")[:2]); return h*60+m2
            except: return -1

        for row in rows:
            empcode    = str(row[0]).strip() if row[0] else ""
            att_date   = str(row[1]).strip() if row[1] else ""
            punch_time = str(row[2]).strip() if row[2] else ""
            in_out_flag= str(row[3]).strip().lower() if row[3] is not None else ""
            if not empcode or not att_date or not punch_time: continue
            if len(punch_time) < 4: continue
            try:
                punch_dt = _dtt.strptime(f"{att_date} {punch_time[:5]}", "%Y-%m-%d %H:%M")
                emp_punches_sql[empcode].append((punch_dt, in_out_flag))
            except: continue

        # ── Save to attendance ─────────────────────────────
        conn = get_db()
        all_shifts_sql = get_all_shifts(conn)
        imported = 0; skipped = 0; unmatched = set()

        for empcode, punch_list in emp_punches_sql.items():
            emp, matched_code = find_emp_by_machine_id(conn, empcode)
            if not emp:
                unmatched.add(empcode); skipped += 1; continue

            ec2      = matched_code
            category = emp["category"]

            # Employee shift group shifts
            grp_shifts = []
            try:
                grp_rows = conn.execute("""SELECT s.* FROM shifts s
                    JOIN shift_group_members sgm ON s.id=sgm.shift_id
                    JOIN employee_shift_groups esg ON sgm.group_id=esg.group_id
                    WHERE esg.emp_code=? AND s.is_active=1""", (ec2,)).fetchall()
                grp_shifts = [dict(r) for r in grp_rows]
            except: pass
            use_shifts = grp_shifts if grp_shifts else all_shifts_sql

            # Check if InOutFlag data is useful
            flags = [f for _,f in punch_list]
            has_flags = any(f in ['0','1','in','out','i','o','entry','exit'] for f in flags)

            if has_flags:
                # Use InOutFlag: sort, pair IN with next OUT
                punch_list_sorted = sorted(punch_list, key=lambda x: x[0])
                pairs = []
                i = 0
                while i < len(punch_list_sorted):
                    pdt, flg = punch_list_sorted[i]
                    is_in  = flg in ['0','in','i','entry','']
                    is_out = flg in ['1','out','o','exit']
                    if is_in:
                        # Find next OUT
                        out_dt = None
                        for j in range(i+1, len(punch_list_sorted)):
                            ndt, nflg = punch_list_sorted[j]
                            if nflg in ['1','out','o','exit']:
                                gap = (ndt - pdt).total_seconds()/60
                                if 5 <= gap <= 24*60:
                                    out_dt = ndt; i = j; break
                        in_date = pdt.strftime("%Y-%m-%d")
                        in_time = pdt.strftime("%H:%M")
                        out_time = out_dt.strftime("%H:%M") if out_dt else ""
                        from_ettrack = etimetrack_pair_punches([pdt] + ([out_dt] if out_dt else []), use_shifts)
                        if from_ettrack:
                            pairs.append(from_ettrack[0])
                        else:
                            pairs.append({"date":in_date,"in_time":in_time,"out_time":out_time,"shift":None,"worked_minutes":0,"ot_minutes":0})
                    i += 1
            else:
                # No flags — use eTimeTrack chronological pairing
                punch_dts = sorted(set(pdt for pdt,_ in punch_list))
                pairs = etimetrack_pair_punches(punch_dts, use_shifts)

            for pair in pairs:
                att_date = pair["date"]
                if not all_months:
                    try:
                        from datetime import date as _dtc2
                        ad = _dtc2.fromisoformat(att_date)
                        if ad.month != month or ad.year != year: continue
                    except: pass

                pi = pair["in_time"]
                po = pair["out_time"]

                try:
                    from datetime import date as _dtc
                    att_dt = _dtc.fromisoformat(att_date)
                    auto_status = "WOP" if att_dt.weekday()==get_emp_weekly_off_num(ec, conn) else "Present"
                    if auto_status == "Present":
                        hol = conn.execute("""SELECT id FROM holidays WHERE holiday_date=?
                            AND (applies_to='All' OR applies_to IS NULL OR applies_to='' OR applies_to=? OR applies_to='Staff' OR applies_to='Associate')""",
                            (att_date, category)).fetchone()
                        if hol:
                            auto_status = "Holiday"
                            # HP: holiday with punch — 30min break, OT counted
                except:
                    auto_status = "Present"
                    hol = None

                # Miss Punch: IN present, OUT missing
                # Past dates → always Miss Punch; Today → after shift end + 30 min
                if pi and not po and auto_status not in ("WOP", "Holiday"):
                    try:
                        from datetime import datetime as _dtnow_db2, date as _date_db2
                        _att_d2 = _date_db2.fromisoformat(att_date)
                        if _att_d2 < _date_db2.today():
                            auto_status = "Miss Punch"
                        else:
                            _now_m2 = _dtnow_db2.now().hour * 60 + _dtnow_db2.now().minute
                            _sh2 = conn.execute("""SELECT s.end_time, s.is_night_shift
                                FROM shifts s JOIN shift_roster_dates srd ON s.id=srd.shift_id
                                WHERE srd.emp_code=? AND srd.shift_date=?""",
                                (ec2, att_date)).fetchone()
                            if _sh2 and _sh2["end_time"]:
                                _eh2, _em2 = map(int, _sh2["end_time"].split(":")[:2])
                                _end_m2 = _eh2*60+_em2
                                if _sh2["is_night_shift"] and _end_m2 < 12*60:
                                    _end_m2 += 24*60
                                if _now_m2 > _end_m2 + 30:
                                    auto_status = "Miss Punch"
                            else:
                                _in_h2, _in_m2x = map(int, str(pi).split(":")[:2])
                                if _now_m2 > _in_h2*60+_in_m2x+9*60:
                                    auto_status = "Miss Punch"
                    except:
                        auto_status = "Miss Punch"

                # Pass status_override for WOP/Holiday so calc_att applies correct OT logic
                _sovrd = None
                if auto_status == "WOP": _sovrd = "WOP"
                elif auto_status == "Holiday" and pi: _sovrd = "Holiday"
                save_att_row(conn, ec2, att_date, pi, po, category,
                            status=auto_status, status_override=_sovrd)
                imported += 1
                if imported % 200 == 0: conn.commit()

        conn.commit()
        conn.close()

        return jsonify({
            "success":         True,
            "imported":        imported,
            "skipped":         skipped,
            "unmatched_count": len(unmatched),
            "unmatched_ids":   sorted(list(unmatched))[:20],
            "total_records":   len(rows),
        })

    except ImportError:
        return jsonify({"success": False,
                        "error": "Please install pyodbc: pip install pyodbc"})
    except pyodbc.Error as e:
        return jsonify({"success": False,
                        "error": f"MS SQL Connection Error: {str(e)}"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/attendance/import-excel",methods=["POST"])
@amgr
def import_att_excel():
    if "file" not in request.files: return jsonify({"success":False,"error":"No file"})
    month=int(request.form.get("month",date.today().month))
    year =int(request.form.get("year", date.today().year))
    try:
        import openpyxl
        wb=openpyxl.load_workbook(io.BytesIO(request.files["file"].read())); ws=wb.active
        hdrs=[str(c.value).strip().lower().replace(" ","_").replace(".","") if c.value else "" for c in ws[1]]
        conn=get_db(); imported=0; errors=[]
        for row in ws.iter_rows(min_row=2,values_only=True):
            if not any(row): continue
            d=dict(zip(hdrs,row))
            # Support your format: Emp. Code, InTime, OutTime, Status
            ecode=str(d.get("emp_code","") or d.get("empcode","") or d.get("emp_code:","") or "").strip()
            dt   =d.get("date","") or d.get("att_date","")
            in_t =str(d.get("intime","") or d.get("in_time","") or d.get("punch_in","") or "").strip()
            out_t=str(d.get("outtime","") or d.get("out_time","") or d.get("punch_out","") or "").strip()
            status=str(d.get("status","Present") or "Present").strip()
            if not ecode or not dt: continue
            if hasattr(dt,"strftime"): dt=dt.strftime("%Y-%m-%d")
            else: dt=str(dt).strip()[:10]
            try:
                dobj=datetime.strptime(dt,"%Y-%m-%d")
                if dobj.month!=month or dobj.year!=year: continue
            except: continue
            emp=conn.execute("SELECT emp_code,category FROM employees WHERE emp_code=?",(ecode,)).fetchone()
            if not emp: errors.append(f"{ecode} not found"); continue
            # Handle WO and WOP statuses
            if status.upper() in ["WO","W.O.","WEEK OFF"]: status="WO"
            elif status.upper() in ["WOP","W.O.P","WEEK OFF PRESENT"]: status="WOP"
            save_att_row(conn,ecode,dt,in_t,out_t,emp["category"],status); imported+=1
        conn.commit(); conn.close()
        return jsonify({"success":True,"imported":imported,"errors":errors[:5]})
    except Exception as e: return jsonify({"success":False,"error":str(e)})

# ─── SALARY ──────────────────────────────────────────

# ─── EMPLOYEE SCHEME MASTER ─────────────────────────────────
@app.route("/payroll/schemes")
@amgr
def payroll_schemes():
    conn = get_db()
    schemes = conn.execute("SELECT * FROM employee_schemes ORDER BY scheme_name").fetchall()
    conn.close()
    return render_template("payroll_schemes.html", schemes=[dict(s) for s in schemes])

@app.route("/payroll/schemes/add", methods=["POST"])
@amgr
def scheme_add():
    d = request.json; conn = get_db()
    try:
        conn.execute("""INSERT INTO employee_schemes
            (scheme_name,description,pf_applicable,esi_applicable,pt_applicable,
             bonus_applicable,gratuity_applicable,lwf_applicable,tds_applicable,ot_applicable,notes,is_active,created_on)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,1,date('now'))""",
            (d["scheme_name"],d.get("description",""),
             1 if d.get("pf_applicable") else 0,
             1 if d.get("esi_applicable") else 0,
             1 if d.get("pt_applicable") else 0,
             1 if d.get("bonus_applicable") else 0,
             1 if d.get("gratuity_applicable") else 0,
             1 if d.get("lwf_applicable") else 0,
             1 if d.get("tds_applicable") else 0,
             1 if d.get("ot_applicable") else 0,
             d.get("notes","")))
        conn.commit()
        return jsonify({"success":True})
    except Exception as e: return jsonify({"success":False,"error":str(e)})
    finally: conn.close()

@app.route("/payroll/schemes/edit/<int:sid>", methods=["POST"])
@amgr
def scheme_edit(sid):
    d = request.json
    if not d: return jsonify({"success":False,"error":"No data received"})
    conn = get_db()
    try:
        rows = conn.execute("SELECT id FROM employee_schemes WHERE id=?", (sid,)).fetchone()
        if not rows: return jsonify({"success":False,"error":f"Scheme id {sid} not found"})
        conn.execute("""UPDATE employee_schemes SET
            scheme_name=?,description=?,pf_applicable=?,esi_applicable=?,pt_applicable=?,
            bonus_applicable=?,gratuity_applicable=?,lwf_applicable=?,tds_applicable=?,
            ot_applicable=?,notes=?,is_active=? WHERE id=?""",
            (d["scheme_name"],d.get("description",""),
             1 if d.get("pf_applicable") else 0,
             1 if d.get("esi_applicable") else 0,
             1 if d.get("pt_applicable") else 0,
             1 if d.get("bonus_applicable") else 0,
             1 if d.get("gratuity_applicable") else 0,
             1 if d.get("lwf_applicable") else 0,
             1 if d.get("tds_applicable") else 0,
             1 if d.get("ot_applicable") else 0,
             d.get("notes",""),
             1 if d.get("is_active",True) else 0, sid))
        conn.commit()
        return jsonify({"success":True})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})
    finally: conn.close()

@app.route("/payroll/schemes/delete/<int:sid>", methods=["POST"])
@amgr
def scheme_delete(sid):
    conn = get_db()
    # Check if any employee uses this scheme
    count = conn.execute("SELECT COUNT(*) FROM employees WHERE scheme_id=?", (sid,)).fetchone()[0]
    if count > 0:
        conn.close()
        return jsonify({"success":False,"error":f"{count} employees use this scheme. Reassign first."})
    conn.execute("DELETE FROM employee_schemes WHERE id=?", (sid,))
    conn.commit(); conn.close()
    return jsonify({"success":True})

@app.route("/payroll/schemes/get-all")
@amgr
def schemes_get_all():
    conn = get_db()
    schemes = conn.execute("SELECT * FROM employee_schemes WHERE is_active=1 ORDER BY scheme_name").fetchall()
    conn.close()
    return jsonify([dict(s) for s in schemes])

# ─── OT RATE MASTER ──────────────────────────────────────────
@app.route("/payroll/ot-rates")
@amgr
def payroll_ot_rates():
    conn = get_db()
    rates = conn.execute("SELECT * FROM ot_rate_master ORDER BY category").fetchall()
    conn.close()
    return render_template("payroll_ot_rates.html", rates=[dict(r) for r in rates])

@app.route("/payroll/ot-rates/save", methods=["POST"])
@amgr
def ot_rate_save():
    d = request.json; conn = get_db()
    try:
        rid = d.get("id")
        if rid:
            conn.execute("""UPDATE ot_rate_master SET category=?,rate_type=?,multiplier=?,
                fixed_rate=?,description=?,is_active=?,updated_on=date('now') WHERE id=?""",
                (d["category"],d.get("rate_type","formula"),
                 float(d.get("multiplier",1.0)),float(d.get("fixed_rate",0)),
                 d.get("description",""),1 if d.get("is_active",True) else 0,rid))
        else:
            conn.execute("""INSERT INTO ot_rate_master
                (category,rate_type,multiplier,fixed_rate,description,is_active,updated_on)
                VALUES (?,?,?,?,?,1,date('now'))""",
                (d["category"],d.get("rate_type","formula"),
                 float(d.get("multiplier",1.0)),float(d.get("fixed_rate",0)),
                 d.get("description","")))
        conn.commit()
        return jsonify({"success":True})
    except Exception as e: return jsonify({"success":False,"error":str(e)})
    finally: conn.close()


# ════════════════════════════════════════════════════════════
# PAYROLL MANAGEMENT ROUTES
# ════════════════════════════════════════════════════════════


# ── Monthly Working Days ────────────────────────
@app.route("/payroll/working-days", methods=["GET","POST"])
@amgr
def payroll_working_days():
    conn = get_db()
    if request.method == "POST":
        d = request.json
        conn.execute("""INSERT INTO monthly_working_days
            (year,month,staff_days,nonstaff_days,notes,created_by,created_on)
            VALUES (?,?,?,?,?,?,datetime('now'))
            ON CONFLICT(year,month) DO UPDATE SET
            staff_days=excluded.staff_days,
            nonstaff_days=excluded.nonstaff_days,
            notes=excluded.notes,
            created_by=excluded.created_by,
            created_on=excluded.created_on""",
            (int(d["year"]),int(d["month"]),
             int(d.get("staff_days",26)),int(d.get("nonstaff_days",26)),
             d.get("notes",""),session.get("name","HR")))
        conn.commit(); conn.close()
        return jsonify({"success":True})
    # Get all records last 24 months + upcoming
    records = conn.execute("""SELECT * FROM monthly_working_days
        ORDER BY year DESC, month DESC LIMIT 36""").fetchall()
    conn.close()
    return render_template("payroll_working_days.html",
        records=[dict(r) for r in records],
        months=MONTHS,
        today_year=date.today().year,
        today_month=date.today().month)

@app.route("/payroll/working-days/delete/<int:rid>", methods=["POST"])
@amgr
def payroll_wd_delete(rid):
    conn = get_db()
    conn.execute("DELETE FROM monthly_working_days WHERE id=?", (rid,))
    conn.commit(); conn.close()
    return jsonify({"success":True})


@app.route("/payroll/migrate")
@amgr  
def payroll_migrate():
    """Run DB migration — adds missing columns to existing database"""
    conn = get_db()
    results = []
    
    # salary_records columns
    salary_cols = [
        "pt REAL DEFAULT 0",
        "lwf REAL DEFAULT 0", 
        "bonus REAL DEFAULT 0",
        "arrears REAL DEFAULT 0",
        "advance_deduction REAL DEFAULT 0",
        "employer_pf REAL DEFAULT 0",
        "employer_esi REAL DEFAULT 0",
        "absent_days REAL DEFAULT 0",
        "paid_leave_days REAL DEFAULT 0",
        "half_days REAL DEFAULT 0",
        "late_marks INTEGER DEFAULT 0",
        "wop_days REAL DEFAULT 0",
        "holiday_days REAL DEFAULT 0",
        "payable_days REAL DEFAULT 0",
        "per_day_salary REAL DEFAULT 0",
        "payment_status TEXT DEFAULT 'Pending'",
        "payment_date TEXT",
        "payment_mode TEXT",
        "payment_ref TEXT",
        "remarks TEXT",
        "locked INTEGER DEFAULT 0",
    ]
    existing = {r[1] for r in conn.execute("PRAGMA table_info(salary_records)").fetchall()}
    added = []
    for col_def in salary_cols:
        col_name = col_def.split()[0]
        if col_name not in existing:
            try:
                conn.execute(f"ALTER TABLE salary_records ADD COLUMN {col_def}")
                added.append(col_name)
            except Exception as e:
                results.append(f"Error adding {col_name}: {e}")
    
    # employees scheme_id
    emp_cols = conn.execute("PRAGMA table_info(employees)").fetchall()
    emp_col_names = {r[1] for r in emp_cols}
    if "scheme_id" not in emp_col_names:
        try:
            conn.execute("ALTER TABLE employees ADD COLUMN scheme_id INTEGER DEFAULT NULL")
            added.append("employees.scheme_id")
        except: pass

    # attendance remarks
    att_cols = {r[1] for r in conn.execute("PRAGMA table_info(attendance)").fetchall()}
    if "remarks" not in att_cols:
        try:
            conn.execute("ALTER TABLE attendance ADD COLUMN remarks TEXT DEFAULT NULL")
            added.append("attendance.remarks")
        except: pass

    # New tables
    conn.execute("""CREATE TABLE IF NOT EXISTS payroll_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_code TEXT, month INTEGER, year INTEGER,
        action TEXT, old_value TEXT, new_value TEXT,
        changed_by TEXT, changed_on TEXT)""")
    
    conn.execute("""CREATE TABLE IF NOT EXISTS payroll_bonus (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_code TEXT NOT NULL, bonus_type TEXT DEFAULT 'festival',
        amount REAL DEFAULT 0, month INTEGER, year INTEGER,
        remarks TEXT, created_by TEXT, created_on TEXT)""")
    
    conn.execute("""CREATE TABLE IF NOT EXISTS payroll_payments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        emp_code TEXT NOT NULL, month INTEGER, year INTEGER,
        net_amount REAL DEFAULT 0, payment_mode TEXT DEFAULT 'Bank Transfer',
        payment_date TEXT, payment_ref TEXT, bank_account TEXT,
        status TEXT DEFAULT 'Pending', processed_by TEXT, processed_on TEXT)""")

    conn.execute("""CREATE TABLE IF NOT EXISTS payroll_settings (
        id INTEGER PRIMARY KEY DEFAULT 1,
        company_name TEXT DEFAULT 'Vijayshri Packaging Ltd.',
        pf_employer_pct REAL DEFAULT 12.0,
        pf_employee_pct REAL DEFAULT 12.0,
        esic_employer_pct REAL DEFAULT 3.25,
        esic_employee_pct REAL DEFAULT 0.75,
        esic_wage_limit REAL DEFAULT 21000,
        pf_wage_limit REAL DEFAULT 15000,
        pt_state TEXT DEFAULT 'Madhya Pradesh',
        pt_applicable INTEGER DEFAULT 0,
        lwf_amount REAL DEFAULT 0,
        working_days_month INTEGER DEFAULT 26,
        ot_rate_formula TEXT DEFAULT 'basic_div_26_div_shift',
        late_grace_minutes INTEGER DEFAULT 15,
        late_halfday_count INTEGER DEFAULT 3,
        el_per_year REAL DEFAULT 16,
        cl_per_year REAL DEFAULT 6,
        payment_day INTEGER DEFAULT 7,
        updated_on TEXT)""")
    conn.execute("INSERT OR IGNORE INTO payroll_settings (id) VALUES (1)")

    conn.execute("""CREATE TABLE IF NOT EXISTS employee_schemes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scheme_name TEXT UNIQUE NOT NULL,
        description TEXT,
        pf_applicable INTEGER DEFAULT 1,
        esi_applicable INTEGER DEFAULT 1,
        pt_applicable INTEGER DEFAULT 0,
        bonus_applicable INTEGER DEFAULT 1,
        gratuity_applicable INTEGER DEFAULT 1,
        lwf_applicable INTEGER DEFAULT 0,
        tds_applicable INTEGER DEFAULT 1,
        ot_applicable INTEGER DEFAULT 1,
        notes TEXT, is_active INTEGER DEFAULT 1, created_on TEXT)""")

    conn.execute("""CREATE TABLE IF NOT EXISTS ot_rate_master (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        rate_type TEXT DEFAULT 'formula',
        multiplier REAL DEFAULT 1.0,
        fixed_rate REAL DEFAULT 0,
        description TEXT,
        is_active INTEGER DEFAULT 1,
        updated_on TEXT)""")
    
    # OT Rate Master — keep only Associate Gross OT
    # Delete all existing, insert only the correct one
    conn.execute("DELETE FROM ot_rate_master")
    conn.execute("""INSERT INTO ot_rate_master
        (category,rate_type,multiplier,fixed_rate,description,is_active,updated_on)
        VALUES ('Associate','gross_ot',1.3,0,'(Actual Gross÷208)×1.3 — OT Rate',1,date('now'))""")

    conn.execute("""CREATE TABLE IF NOT EXISTS monthly_working_days (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        year INTEGER NOT NULL, month INTEGER NOT NULL,
        staff_days INTEGER DEFAULT 26, nonstaff_days INTEGER DEFAULT 26,
        notes TEXT, created_by TEXT, created_on TEXT,
        UNIQUE(year, month))""")

    conn.execute("""CREATE TABLE IF NOT EXISTS pt_slabs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        state TEXT DEFAULT 'Madhya Pradesh',
        salary_from REAL DEFAULT 0, salary_to REAL DEFAULT 0,
        pt_amount REAL DEFAULT 0, frequency TEXT DEFAULT 'monthly',
        is_active INTEGER DEFAULT 1)""")
    if conn.execute("SELECT COUNT(*) FROM pt_slabs").fetchone()[0] == 0:
        for sf,st,amt in [(0,18999,0),(19000,25000,208),(25001,99999999,212)]:
            conn.execute("INSERT INTO pt_slabs (state,salary_from,salary_to,pt_amount,is_active) VALUES ('Madhya Pradesh',?,?,?,1)",(sf,st,amt))

    conn.commit(); conn.close()
    
    return jsonify({
        "success": True,
        "added_columns": added,
        "message": f"Migration complete. Added: {', '.join(added) if added else 'Nothing new (all up to date)'}"
    })


# ════════════════════════════════════════════════════════════
# OT PROCESS — Separate from Payroll
# ════════════════════════════════════════════════════════════


@app.route("/payroll/trends")
@amgr
def payroll_trends():
    """Payroll analytics trend charts page"""
    conn = get_db()
    depts = [d["department"] for d in conn.execute(
        "SELECT DISTINCT department FROM employees WHERE status='Active' AND department IS NOT NULL ORDER BY department"
    ).fetchall()]
    conn.close()
    return render_template("payroll_trends.html", departments=depts, months=MONTHS)

@app.route("/payroll/ot-process")
@amgr
def ot_process_page():
    """OT Process page — separate from main salary"""
    m = int(request.args.get("month", date.today().month))
    y = int(request.args.get("year",  date.today().year))
    conn = get_db()

    # Get OT data from attendance
    ot_rows = conn.execute("""SELECT a.emp_code, e.emp_name, e.department, e.category,
        e.basic, e.hra, e.special_allowance,
        SUM(a.ot_minutes) as total_ot_min,
        COUNT(CASE WHEN a.ot_minutes > 0 THEN 1 END) as ot_days,
        COUNT(CASE WHEN a.status='WOP' THEN 1 END) as wop_days,
        COUNT(CASE WHEN a.status='Holiday' AND a.ot_minutes > 0 THEN 1 END) as hp_days
        FROM attendance a
        JOIN employees e ON a.emp_code=e.emp_code
        WHERE strftime('%m',a.att_date)=? AND strftime('%Y',a.att_date)=?
        AND e.status='Active' AND e.category='Associate'
        GROUP BY a.emp_code
        HAVING total_ot_min > 0
        ORDER BY e.department, e.emp_name""",
        (f"{m:02d}", str(y))).fetchall()

    # Get OT rate for Associate
    ot_rate = conn.execute("""SELECT * FROM ot_rate_master
        WHERE category='Associate' AND is_active=1 LIMIT 1""").fetchone()

    # Calculate OT amount for each employee
    ot_data = []
    total_ot_amount = 0
    for r in ot_rows:
        gross_base = float(r["basic"] or 0) + float(r["hra"] or 0) + float(r["special_allowance"] or 0)
        ot_hrs = round((r["total_ot_min"] or 0) / 60, 2)

        if ot_rate:
            rt = ot_rate["rate_type"]
            if rt == "gross_ot":
                ot_amount = round((gross_base / 208) * 1.3 * ot_hrs, 2)
            elif rt == "gross_single_ot":
                ot_amount = round((gross_base / 208) * ot_hrs, 2)
            elif rt == "formula":
                ot_amount = round((float(r["basic"] or 0) / 26 / 8.5) * ot_hrs, 2)
            else:
                ot_amount = 0
        else:
            ot_amount = round((gross_base / 208) * 1.3 * ot_hrs, 2)

        # ESIC on OT — deduct if gross_base <= 21000
        esic_on_ot = 0
        if gross_base <= 21000 and ot_amount > 0:
            esic_on_ot = round(ot_amount * 0.0075, 2)

        ot_net = round(ot_amount - esic_on_ot, 2)

        row = dict(r)
        row["ot_hours"]    = ot_hrs
        row["ot_amount"]   = ot_amount
        row["esic_on_ot"]  = esic_on_ot
        row["ot_net"]      = ot_net
        row["gross_base"]  = gross_base
        total_ot_amount   += ot_net
        ot_data.append(row)

    conn.close()
    return render_template("ot_process.html",
        ot_data=ot_data,
        ot_rate=dict(ot_rate) if ot_rate else {},
        total_ot=total_ot_amount,
        month=m, year=y, month_name=MONTHS[m-1],
        months=MONTHS)

@app.route("/payroll/ot-export/<int:m>/<int:y>")
@amgr
def ot_export(m, y):
    """Export OT report to Excel"""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    conn = get_db()
    ot_rows = conn.execute("""SELECT a.emp_code, e.emp_name, e.department, e.category,
        e.basic, e.hra, e.special_allowance,
        SUM(a.ot_minutes) as total_ot_min,
        COUNT(CASE WHEN a.status='WOP' THEN 1 END) as wop_days,
        COUNT(CASE WHEN a.status='Holiday' AND a.ot_minutes > 0 THEN 1 END) as hp_days
        FROM attendance a
        JOIN employees e ON a.emp_code=e.emp_code
        WHERE strftime('%m',a.att_date)=? AND strftime('%Y',a.att_date)=?
        AND e.status='Active' AND e.category='Associate'
        GROUP BY a.emp_code
        HAVING total_ot_min > 0
        ORDER BY e.department, e.emp_name""",
        (f"{m:02d}", str(y))).fetchall()

    ot_rate = conn.execute("SELECT * FROM ot_rate_master WHERE category='Associate' AND is_active=1 LIMIT 1").fetchone()
    conn.close()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"OT Report {MONTHS[m-1]} {y}"

    ws.merge_cells("A1:I1")
    ws["A1"] = f"VIJAYSHRI PACKAGING LTD. — OT Report | {MONTHS[m-1]} {y}"
    ws["A1"].font = Font(bold=True, size=12, color="FFFFFF")
    ws["A1"].fill = PatternFill("solid", fgColor="0052CC")
    ws["A1"].alignment = Alignment(horizontal="center")

    hdrs = ["Emp Code","Name","Department","Gross Base","OT Hours","OT Amount","WOP Days","Holiday Present","Category"]
    ws.append(hdrs)
    for cell in ws[2]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1E3A5F")

    total = 0
    for r in ot_rows:
        gross = float(r["basic"] or 0) + float(r["hra"] or 0) + float(r["special_allowance"] or 0)
        ot_hrs = round((r["total_ot_min"] or 0) / 60, 2)
        ot_amt = round((gross / 208) * 1.3 * ot_hrs, 2) if ot_rate else 0
        total += ot_amt
        ws.append([r["emp_code"], r["emp_name"], r["department"],
                  gross, ot_hrs, ot_amt,
                  r["wop_days"], r["hp_days"], r["category"]])

    ws.append(["","","TOTAL","","",total,"","",""])
    ws.cell(ws.max_row, 3).font = Font(bold=True)
    ws.cell(ws.max_row, 6).font = Font(bold=True)

    for i,w in enumerate([10,25,18,14,10,14,10,14,12],1):
        ws.column_dimensions[get_column_letter(i)].width = w

    return xlresp(wb, f"OT_Report_{MONTHS[m-1]}_{y}.xlsx")


# ════════════════════════════════════════════════════════════
# LEAVE (ASSOCIATE) MODULE — inside OT Process
# ════════════════════════════════════════════════════════════

@app.route("/payroll/leave-associate")
@amgr
def leave_associate_page():
    """Leave Associate — month-wise absent data + approval + OT impact"""
    m      = int(request.args.get("month", date.today().month))
    y      = int(request.args.get("year",  date.today().year))
    dept   = request.args.get("dept", "")
    scheme = request.args.get("scheme", "")
    conn   = get_db()

    # Step 1: Get ALL absent days for Associate from attendance
    import calendar as _cal
    # Two filter variants: with alias (for JOIN queries) and without (for direct table queries)
    dept_filter      = " AND e.department=?" if dept else ""       # for JOIN queries
    dept_filter_bare = " AND department=?"   if dept else ""       # for direct table queries
    dept_params      = [dept] if dept else []
    scheme_filter      = ""; scheme_filter_bare = ""; scheme_params = []
    if scheme:
        _sc = conn.execute("SELECT id FROM employee_schemes WHERE scheme_name=? AND is_active=1",(scheme,)).fetchone()
        if _sc:
            scheme_filter      = " AND e.scheme_id=?"; scheme_filter_bare = " AND scheme_id=?"
            scheme_params = [_sc["id"]]

    absent_rows = conn.execute(f"""
        SELECT a.emp_code, a.att_date, a.status,
               e.emp_name, e.department, e.category, e.esic_number,
               e.basic, e.hra, e.special_allowance
        FROM attendance a
        JOIN employees e ON a.emp_code=e.emp_code
        WHERE strftime('%m',a.att_date)=?
          AND strftime('%Y',a.att_date)=?
          AND e.category='Associate'
          AND a.status='Absent'{dept_filter}{scheme_filter}
        ORDER BY a.emp_code, a.att_date
    """, [f"{m:02d}", str(y)] + dept_params + scheme_params).fetchall()

    # Step 1b: Also find employees with NO attendance record at all
    all_nonstaff = conn.execute(f"""
        SELECT emp_code, emp_name, department, category, esic_number, basic, hra, special_allowance
        FROM employees WHERE status='Active' AND category='Associate'{dept_filter_bare}{scheme_filter_bare}
        ORDER BY emp_code
    """, dept_params + scheme_params).fetchall()

    # Step 2: Auto-insert leave records ONLY for actual Absent days in attendance
    # emps_with_att = employees who have ANY attendance record this month
    all_att_emps = conn.execute("""SELECT DISTINCT emp_code FROM attendance
        WHERE strftime('%m',att_date)=? AND strftime('%Y',att_date)=?""",
        (f"{m:02d}", str(y))).fetchall()
    emps_with_att = set(r["emp_code"] for r in all_att_emps)

    # For employees with NO attendance at all → insert absent records for all working days
    from datetime import date as _dt, timedelta as _td
    total_days = _cal.monthrange(y, m)[1]
    hols = {h["holiday_date"] for h in conn.execute(
        "SELECT holiday_date FROM holidays WHERE strftime('%m',holiday_date)=? AND strftime('%Y',holiday_date)=?",
        (f"{m:02d}", str(y))).fetchall()}

    for emp in all_nonstaff:
        ec = emp["emp_code"]
        if ec in emps_with_att:
            # Has attendance records — only absent_rows (from query) will be inserted
            continue
        # No attendance at all → all working days = absent
        for day in range(1, total_days + 1):
            d_obj = _dt(y, m, day)
            if d_obj.weekday() == get_emp_weekly_off_num(ec, conn): continue
            dt_str = d_obj.strftime("%Y-%m-%d")
            if dt_str in hols: continue
            conn.execute("""INSERT OR IGNORE INTO associate_leave_records
                (emp_code, emp_name, month, year, absent_date, leave_status)
                VALUES (?,?,?,?,?,'Pending')""",
                (ec, emp["emp_name"], m, y, dt_str))

    # Insert leave records for employees WITH attendance — only actual Absent days
    for r in absent_rows:
        conn.execute("""INSERT OR IGNORE INTO associate_leave_records
            (emp_code, emp_name, month, year, absent_date, leave_status)
            VALUES (?,?,?,?,?,'Pending')""",
            (r["emp_code"], r["emp_name"], m, y, r["att_date"]))
    conn.commit()

    # Step 3: Get all leave records with approval status for this month
    # Include ALL employees (active + inactive) who have records for this month
    records = conn.execute(f"""
        SELECT lr.*, e.emp_name, e.department, e.category, e.esic_number,
               e.basic, e.hra, e.special_allowance
        FROM associate_leave_records lr
        JOIN employees e ON lr.emp_code=e.emp_code
        WHERE lr.month=? AND lr.year=?{dept_filter}{scheme_filter}
          AND e.category='Associate'
        ORDER BY lr.emp_code, lr.absent_date
    """, [m, y] + dept_params + scheme_params).fetchall()

    # Step 4: Group by employee + calculate OT impact
    from collections import defaultdict
    emp_map = defaultdict(lambda: {
        "emp_code":"","emp_name":"","department":"","esic_number":"",
        "basic":0,"hra":0,"special_allowance":0,
        "absent_dates":[],"approved":0,"pending":0,"rejected":0,
        "total_absent":0
    })

    # FIRST: Pre-populate emp_map with ALL active Associates (even 100% present ones)
    # so no employee is missed from OT calculation
    for emp in all_nonstaff:
        ec = emp["emp_code"]
        emp_map[ec]["emp_code"]    = ec
        emp_map[ec]["emp_name"]    = emp["emp_name"]
        emp_map[ec]["department"]  = emp["department"] or ""
        emp_map[ec]["esic_number"] = emp["esic_number"] or ""
        emp_map[ec]["basic"]       = float(emp["basic"] or 0)
        emp_map[ec]["hra"]         = float(emp["hra"] or 0)
        emp_map[ec]["special_allowance"] = float(emp["special_allowance"] or 0)

    for r in records:
        ec = r["emp_code"]
        emp_map[ec]["emp_code"]    = ec
        emp_map[ec]["emp_name"]    = r["emp_name"]
        emp_map[ec]["department"]  = r["department"]
        emp_map[ec]["esic_number"] = r["esic_number"] or ""
        emp_map[ec]["basic"]       = float(r["basic"] or 0)
        emp_map[ec]["hra"]         = float(r["hra"] or 0)
        emp_map[ec]["special_allowance"] = float(r["special_allowance"] or 0)
        emp_map[ec]["absent_dates"].append({
            "id": r["id"],
            "date": r["absent_date"],
            "status": r["leave_status"],
            "approved_by": r["approved_by"] or "",
            "approved_on": r["approved_on"] or "",
            "remarks": r["remarks"] or ""
        })
        emp_map[ec]["total_absent"] += 1
        if r["leave_status"] == "Approved":   emp_map[ec]["approved"] += 1
        elif r["leave_status"] == "Rejected": emp_map[ec]["rejected"] += 1
        else:                                  emp_map[ec]["pending"]  += 1

    # Step 5: Get OT data for each employee (all Associates this month)
    ot_rows = conn.execute("""
        SELECT a.emp_code, SUM(a.ot_minutes) as total_ot_min
        FROM attendance a
        JOIN employees e ON a.emp_code=e.emp_code
        WHERE strftime('%m',a.att_date)=? AND strftime('%Y',a.att_date)=?
          AND e.category='Associate'
        GROUP BY a.emp_code
    """, (f"{m:02d}", str(y))).fetchall()
    ot_by_emp = {r["emp_code"]: (r["total_ot_min"] or 0) for r in ot_rows}

    # Step 6: OT rates
    ot_rate_normal = conn.execute(
        "SELECT * FROM ot_rate_master WHERE category='Associate' AND rate_type='gross_ot' AND is_active=1 LIMIT 1"
    ).fetchone()
    ot_rate_single = conn.execute(
        "SELECT * FROM ot_rate_master WHERE category='Associate' AND rate_type='gross_single_ot' AND is_active=1 LIMIT 1"
    ).fetchone()

    # Step 7: Calculate OT impact per employee
    #
    # DEFAULT: All OT goes to Unauth bucket until approved
    # Approved leave → that day's OT shifts to Approved bucket
    #
    # Logic:
    #   unauth_days  = total_absent - approved  (pending + rejected = unauth)
    #   unauth_ot    = min(actual_ot, 8 × unauth_days)
    #   approved_ot  = actual_ot - unauth_ot  (can be 0 if actual < unauth)
    #
    # Case 1: actual=100, unauth=2 → unauth_ot=16, approved_ot=84
    # Case 2: actual=50,  unauth=7 → unauth_ot=50 (cap at actual), approved_ot=0
    #
    # Pay: approved_ot × (Gross/208×1.3) + unauth_ot × (Gross/208)

    emp_list = []
    total_ot_pay = 0
    for ec, emp in emp_map.items():
        gross_base = emp["basic"] + emp["hra"] + emp["special_allowance"]

        # Unauth = not approved (pending + rejected)
        unauth_days   = emp["pending"] + emp["rejected"]
        approved_days = emp["approved"]

        actual_ot_min = ot_by_emp.get(ec, 0)
        actual_ot_hrs = round(actual_ot_min / 60, 2)

        # Unauth OT = 8 × unauth_days, capped at actual OT
        unauth_ot_raw = 8 * unauth_days
        unauth_ot_hrs = min(actual_ot_hrs, unauth_ot_raw)

        # Approved OT = remainder after unauth
        approved_ot_hrs = max(0, round(actual_ot_hrs - unauth_ot_hrs, 2))

        # Pay rates
        ot_rate_val    = (gross_base / 208) * 1.3   # OT rate
        single_rate_val = gross_base / 208            # Single OT rate

        approved_ot_pay = round(ot_rate_val * approved_ot_hrs, 2)
        unauth_ot_pay   = round(single_rate_val * unauth_ot_hrs, 2)
        net_ot_pay      = round(approved_ot_pay + unauth_ot_pay, 2)

        # ESIC on OT
        esic_on_ot   = round(net_ot_pay * 0.0075, 2) if gross_base <= 21000 else 0
        net_ot_final = round(net_ot_pay - esic_on_ot, 2)

        emp["gross_base"]       = gross_base
        emp["actual_ot_hrs"]    = actual_ot_hrs
        emp["approved_ot_hrs"]  = approved_ot_hrs
        emp["approved_ot_pay"]  = approved_ot_pay
        emp["unauth_days"]      = unauth_days
        emp["unauth_ot_hrs"]    = unauth_ot_hrs
        emp["unauth_ot_pay"]    = unauth_ot_pay
        emp["net_ot_hrs"]       = round(approved_ot_hrs + unauth_ot_hrs, 2)
        emp["net_ot_pay"]       = net_ot_pay
        emp["esic_on_ot"]       = esic_on_ot
        emp["net_ot_final"]     = net_ot_final
        total_ot_pay += net_ot_final
        emp_list.append(emp)

    emp_list.sort(key=lambda x: x["emp_code"])

    # Summary
    summary = {
        "total_emps":        len(emp_list),
        "total_absent":      sum(e["total_absent"]    for e in emp_list),
        "total_approved":    sum(e["approved"]        for e in emp_list),
        "total_rejected":    sum(e["rejected"]        for e in emp_list),
        "total_pending":     sum(e["pending"]         for e in emp_list),
        "total_ot_pay":      total_ot_pay,
        "total_actual_ot":   round(sum(e["actual_ot_hrs"]   for e in emp_list), 2),
        "total_approved_ot": round(sum(e["approved_ot_hrs"] for e in emp_list), 2),
        "total_unauth_ot":   round(sum(e["unauth_ot_hrs"]   for e in emp_list), 2),
    }

    depts = conn.execute(
        "SELECT DISTINCT department FROM employees WHERE status='Active' AND category='Associate' AND department IS NOT NULL ORDER BY department"
    ).fetchall()
    schemes_list = conn.execute("SELECT scheme_name FROM employee_schemes WHERE is_active=1 ORDER BY scheme_name").fetchall()
    try:
        lock_rec = conn.execute("SELECT id FROM ot_payment_locks WHERE month=? AND year=?", (m,y)).fetchone()
        is_locked = bool(lock_rec)
    except: is_locked = False  # table not yet created
    conn.close()
    return render_template("leave_associate.html",
        emp_list=emp_list,
        summary=summary,
        month=m, year=y, month_name=MONTHS[m-1],
        months=MONTHS,
        departments=[d["department"] for d in depts],
        schemes=[s["scheme_name"] for s in schemes_list],
        selected_dept=dept,
        selected_scheme=scheme,
        is_locked=is_locked)



@app.route("/payroll/leave-associate/sync", methods=["POST"])
@amgr
def leave_associate_sync():
    """Sync leave records with actual attendance — remove wrong records"""
    d = request.json
    m = int(d.get("month", date.today().month))
    y = int(d.get("year",  date.today().year))
    conn = get_db()
    try:
        # Get all actual absent dates from attendance for Associate
        actual_absent = conn.execute("""
            SELECT a.emp_code, a.att_date FROM attendance a
            JOIN employees e ON a.emp_code=e.emp_code
            WHERE strftime('%m',a.att_date)=? AND strftime('%Y',a.att_date)=?
            AND e.category='Associate' AND e.status='Active'
            AND a.status='Absent'
        """, (f"{m:02d}", str(y))).fetchall()
        actual_set = {(r["emp_code"], r["att_date"]) for r in actual_absent}

        # Also include employees with zero attendance (all working days absent)
        import calendar as _cal2
        from datetime import date as _dt2, timedelta as _td2
        all_emps = conn.execute(
            "SELECT emp_code, emp_name FROM employees WHERE status='Active' AND category='Associate'"
        ).fetchall()
        hols = {h["holiday_date"] for h in conn.execute(
            "SELECT holiday_date FROM holidays WHERE strftime('%m',holiday_date)=? AND strftime('%Y',holiday_date)=?",
            (f"{m:02d}", str(y))).fetchall()}
        emps_with_att = set(r["emp_code"] for r in conn.execute(
            "SELECT DISTINCT emp_code FROM attendance WHERE strftime('%m',att_date)=? AND strftime('%Y',att_date)=?",
            (f"{m:02d}", str(y))).fetchall())

        total_days = _cal2.monthrange(y, m)[1]
        for emp in all_emps:
            ec = emp["emp_code"]
            if ec not in emps_with_att:
                for day in range(1, total_days+1):
                    d_obj = _dt2(y, m, day)
                    if d_obj.weekday() == get_emp_weekly_off_num(ec, conn): continue
                    dt_str = d_obj.strftime("%Y-%m-%d")
                    if dt_str in hols: continue
                    actual_set.add((ec, dt_str))

        # Delete leave records that don't match actual absent days
        # (keep Approved ones even if attendance changed)
        wrong = conn.execute("""
            SELECT id, emp_code, absent_date FROM associate_leave_records
            WHERE month=? AND year=? AND leave_status='Pending'
        """, (m, y)).fetchall()

        deleted = 0
        for r in wrong:
            if (r["emp_code"], r["absent_date"]) not in actual_set:
                conn.execute("DELETE FROM associate_leave_records WHERE id=?", (r["id"],))
                deleted += 1

        conn.commit()
        conn.close()
        return jsonify({"success": True, "deleted": deleted,
                       "message": f"Synced — removed {deleted} wrong records"})
    except Exception as e:
        conn.close()
        return jsonify({"success": False, "error": str(e)})

@app.route("/payroll/leave-associate/approve", methods=["POST"])
@amgr
def leave_associate_approve():
    """Approve/Reject leave records — single or bulk"""
    d = request.json
    conn = get_db()
    try:
        ids     = d.get("ids", [])
        action  = d.get("action", "Approved")  # Approved / Rejected
        remarks = d.get("remarks", "")
        if not ids:
            return jsonify({"success":False,"error":"No records selected"})
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        by  = session.get("name","HR")
        for lid in ids:
            conn.execute("""UPDATE associate_leave_records
                SET leave_status=?, approved_by=?, approved_on=?, remarks=?
                WHERE id=?""",
                (action, by, now, remarks, lid))
        conn.commit()
        conn.close()
        return jsonify({"success":True,
                       "message":f"{len(ids)} record(s) marked as {action}"})
    except Exception as e:
        conn.close()
        return jsonify({"success":False,"error":str(e)})



@app.route("/payroll/leave-associate/save", methods=["POST"])
@amgr
def leave_associate_save():
    """Save current OT snapshot — locks approved/unauth state for this month"""
    d = request.json
    m = int(d.get("month", date.today().month))
    y = int(d.get("year",  date.today().year))
    conn = get_db()
    try:
        # Create snapshot table if not exists
        conn.execute("""CREATE TABLE IF NOT EXISTS associate_ot_snapshot (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            emp_code TEXT, emp_name TEXT, department TEXT,
            month INTEGER, year INTEGER,
            total_absent INTEGER, approved_days INTEGER, unauth_days INTEGER,
            actual_ot_hrs REAL, approved_ot_hrs REAL, unauth_ot_hrs REAL,
            gross_base REAL, approved_ot_pay REAL, unauth_ot_pay REAL,
            net_ot_pay REAL, esic REAL, net_ot_final REAL,
            saved_on TEXT, saved_by TEXT,
            UNIQUE(emp_code, month, year))""")

        # Get records
        records = conn.execute("""
            SELECT lr.emp_code, e.emp_name, e.department,
                   e.basic, e.hra, e.special_allowance,
                   COUNT(*) as total_absent,
                   SUM(CASE WHEN lr.leave_status='Approved' THEN 1 ELSE 0 END) as approved,
                   SUM(CASE WHEN lr.leave_status!='Approved' THEN 1 ELSE 0 END) as unauth
            FROM associate_leave_records lr
            JOIN employees e ON lr.emp_code=e.emp_code
            WHERE lr.month=? AND lr.year=?
            GROUP BY lr.emp_code
        """, (m, y)).fetchall()

        ot_rows = conn.execute("""
            SELECT emp_code, SUM(ot_minutes) as total_ot_min
            FROM attendance WHERE strftime('%m',att_date)=? AND strftime('%Y',att_date)=?
            GROUP BY emp_code
        """, (f"{m:02d}", str(y))).fetchall()
        ot_by_emp = {r["emp_code"]: (r["total_ot_min"] or 0) for r in ot_rows}

        saved = 0
        now = datetime.now().strftime("%Y-%m-%d %H:%M")
        by  = session.get("name","HR")
        for r in records:
            gb = float(r["basic"] or 0)+float(r["hra"] or 0)+float(r["special_allowance"] or 0)
            act_ot_hrs = round(ot_by_emp.get(r["emp_code"],0)/60, 2)
            unauth_days = r["unauth"]
            unauth_ot_hrs = min(act_ot_hrs, 8*unauth_days)
            approved_ot_hrs = max(0, round(act_ot_hrs - unauth_ot_hrs, 2))
            approved_pay = round((gb/208)*1.3*approved_ot_hrs, 2)
            unauth_pay   = round((gb/208)*unauth_ot_hrs, 2)
            net_pay = round(approved_pay+unauth_pay, 2)
            esic = round(net_pay*0.0075,2) if gb<=21000 else 0
            net_final = round(net_pay-esic, 2)

            conn.execute("""INSERT OR REPLACE INTO associate_ot_snapshot
                (emp_code,emp_name,department,month,year,
                 total_absent,approved_days,unauth_days,
                 actual_ot_hrs,approved_ot_hrs,unauth_ot_hrs,
                 gross_base,approved_ot_pay,unauth_ot_pay,
                 net_ot_pay,esic,net_ot_final,saved_on,saved_by)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (r["emp_code"],r["emp_name"],r["department"],m,y,
                 r["total_absent"],r["approved"],unauth_days,
                 act_ot_hrs,approved_ot_hrs,unauth_ot_hrs,
                 gb,approved_pay,unauth_pay,
                 net_pay,esic,net_final,now,by))
            saved += 1
        conn.commit()
        conn.close()
        return jsonify({"success":True, "saved":saved,
                       "message":f"✅ Saved OT snapshot for {MONTHS[m-1]} {y} — {saved} employees"})
    except Exception as e:
        conn.close()
        return jsonify({"success":False,"error":str(e)})

@app.route("/payroll/leave-associate/export/<int:m>/<int:y>")
@amgr
def leave_associate_export(m, y):
    """Export Leave Associate OT report to Excel — with dept/scheme/category/ESIC filters"""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    from collections import defaultdict

    dept   = request.args.get("dept","")
    scheme = request.args.get("scheme","")
    conn   = get_db()

    dept_filter   = " AND e.department=?" if dept else ""
    dept_params   = [dept] if dept else []
    scheme_filter = ""; scheme_params = []
    if scheme:
        _sc = conn.execute("SELECT id FROM employee_schemes WHERE scheme_name=? AND is_active=1",(scheme,)).fetchone()
        if _sc: scheme_filter = " AND e.scheme_id=?"; scheme_params = [_sc["id"]]

    records = conn.execute(f"""
        SELECT lr.*, e.emp_name, e.department, e.category, e.esic_number,
               e.basic, e.hra, e.special_allowance,
               COALESCE(es.scheme_name,'') as scheme_name
        FROM associate_leave_records lr
        JOIN employees e ON lr.emp_code=e.emp_code
        LEFT JOIN employee_schemes es ON e.scheme_id=es.id
        WHERE lr.month=? AND lr.year=?{dept_filter}{scheme_filter}
          AND e.category='Associate'
        ORDER BY lr.emp_code, lr.absent_date
    """, [m, y] + dept_params + scheme_params).fetchall()

    ot_rows = conn.execute("""
        SELECT a.emp_code, SUM(a.ot_minutes) as total_ot_min
        FROM attendance a
        JOIN employees e ON a.emp_code=e.emp_code
        WHERE strftime('%m',a.att_date)=? AND strftime('%Y',a.att_date)=?
          AND e.category='Associate'
        GROUP BY a.emp_code
    """, (f"{m:02d}", str(y))).fetchall()
    ot_by_emp = {r["emp_code"]: (r["total_ot_min"] or 0) for r in ot_rows}

    wb = openpyxl.Workbook(); ws = wb.active
    ws.title = f"Leave Associate {MONTHS[m-1]} {y}"

    ws.merge_cells("A1:S1")
    ws["A1"] = f"VIJAYSHRI PACKAGING LTD. — Leave Associate OT Report | {MONTHS[m-1]} {y}"
    ws["A1"].font = Font(bold=True, size=11, color="FFFFFF")
    ws["A1"].fill = PatternFill("solid", fgColor="0052CC")
    ws["A1"].alignment = Alignment(horizontal="center")

    hdrs = ["Code","Name","Department","Category","Scheme","ESIC Number",
            "Total Absent","Approved","Unauth Days","Pending",
            "Actual OT Hrs","Unauth OT Hrs","Approved OT Hrs",
            "Approved OT Pay","Unauth OT Pay","Gross Base","Total OT Pay","ESIC on OT","Net OT Pay"]
    for ci,h in enumerate(hdrs,1):
        cell = ws.cell(2,ci,h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1E3A5F")
        cell.alignment = Alignment(horizontal="center")

    # Group records by emp
    # Build emp_map from ALL active Associates (including zero-absent ones)
    all_assoc_exp = conn.execute(f"""
        SELECT e.emp_code, e.emp_name, e.department, e.category, e.esic_number,
               e.basic, e.hra, e.special_allowance,
               COALESCE(es.scheme_name,'') as scheme_name
        FROM employees e LEFT JOIN employee_schemes es ON e.scheme_id=es.id
        WHERE e.status='Active' AND e.category='Associate'{dept_filter}{scheme_filter}
        ORDER BY e.emp_code
    """, dept_params + scheme_params).fetchall()
    conn.close()

    emp_map = {e["emp_code"]: {
        "emp_name":          e["emp_name"],
        "department":        e["department"] or "",
        "category":          e["category"] or "Associate",
        "esic_number":       e["esic_number"] or "",
        "scheme_name":       e["scheme_name"] or "",
        "basic":             float(e["basic"] or 0),
        "hra":               float(e["hra"] or 0),
        "special_allowance": float(e["special_allowance"] or 0),
        "approved":0,"rejected":0,"pending":0,"total":0
    } for e in all_assoc_exp}

    for r in records:
        ec=r["emp_code"]
        if ec not in emp_map: continue
        emp_map[ec]["total"]+=1
        if r["leave_status"]=="Approved": emp_map[ec]["approved"]+=1
        elif r["leave_status"]=="Rejected": emp_map[ec]["rejected"]+=1
        else: emp_map[ec]["pending"]+=1

    row_num=3; total_net=0
    for ec,e in sorted(emp_map.items()):
        gb=e["basic"]+e["hra"]+e["special_allowance"]
        act_ot_min=ot_by_emp.get(ec,0)
        act_ot_hrs=round(act_ot_min/60,2)
        unauth_days = e["pending"] + e["rejected"]
        unauth_ot_hrs = min(act_ot_hrs, 8*unauth_days)
        approved_ot_hrs = max(0, round(act_ot_hrs - unauth_ot_hrs, 2))
        approved_ot_pay = round((gb/208)*1.3*approved_ot_hrs, 2)
        unauth_ot_pay   = round((gb/208)*unauth_ot_hrs, 2)
        ot_pay = round(approved_ot_pay + unauth_ot_pay, 2)
        esic_ot=round(ot_pay*0.0075,2) if gb<=21000 else 0
        net=round(ot_pay-esic_ot,2); total_net+=net

        ws.append([ec,e["emp_name"],e["department"],e["category"],e["scheme_name"],e["esic_number"],
                   e["total"],e["approved"],unauth_days,e["pending"],
                   act_ot_hrs,unauth_ot_hrs,approved_ot_hrs,
                   approved_ot_pay,unauth_ot_pay,gb,ot_pay,esic_ot,net])
        row_num+=1

    ws.append(["","","TOTAL","","","","","","","","","","","",
                "","","","",total_net])
    ws.cell(row_num,19).font=Font(bold=True)

    for i,w in enumerate([10,25,15,12,14,16,12,10,10,10,12,12,14,13,11,14,12,10,12],1):
        ws.column_dimensions[get_column_letter(i)].width=w

    return xlresp(wb, f"Leave_Associate_OT_{MONTHS[m-1]}_{y}.xlsx")

@app.route("/payroll/settings", methods=["GET","POST"])
@amgr
def payroll_settings():
    conn = get_db()
    if request.method == "POST":
        d = request.json
        conn.execute("""UPDATE payroll_settings SET
            pf_employer_pct=?,pf_employee_pct=?,
            esic_employer_pct=?,esic_employee_pct=?,esic_wage_limit=?,
            pf_wage_limit=?,pt_state=?,pt_applicable=?,lwf_amount=?,
            working_days_month=?,late_grace_minutes=?,late_halfday_count=?,
            late_free_days=?,short_time_limit_hrs=?,short_time_per_halfday=?,
            el_per_year=?,cl_per_year=?,payment_day=?,ot_rate_formula=?,
            min_basic=?,updated_on=datetime('now') WHERE id=1""",
            (float(d.get("pf_employer_pct",12)),float(d.get("pf_employee_pct",12)),
             float(d.get("esic_employer_pct",3.25)),float(d.get("esic_employee_pct",0.75)),
             float(d.get("esic_wage_limit",21000)),float(d.get("pf_wage_limit",15000)),
             d.get("pt_state","Madhya Pradesh"),1 if d.get("pt_applicable") else 0,
             float(d.get("lwf_amount",0)),int(d.get("working_days_month",26)),
             int(d.get("late_grace_minutes",15)),int(d.get("late_halfday_count",3)),
             int(d.get("late_free_days",2)),
             float(d.get("short_time_limit_hrs",5.0)),
             float(d.get("short_time_per_halfday",2.5)),
             float(d.get("el_per_year",16)),float(d.get("cl_per_year",6)),
             int(d.get("payment_day",7)),d.get("ot_rate_formula","basic_div_26_div_shift"),
             float(d.get("min_basic",0))))
        conn.commit(); conn.close()
        return jsonify({"success":True})
    s = get_payroll_settings(conn)
    pt_slabs = conn.execute("SELECT * FROM pt_slabs WHERE is_active=1 ORDER BY salary_from").fetchall()
    conn.close()
    return render_template("payroll_settings.html", settings=s,
        pt_slabs=[dict(r) for r in pt_slabs])


@app.route("/payroll/debug/<emp_code>/<int:m>/<int:y>")
@amgr
def payroll_debug(emp_code, m, y):
    """Debug salary calculation for one employee"""
    import traceback
    conn = get_db()
    try:
        emp = conn.execute("SELECT * FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
        if not emp:
            return jsonify({"error": f"Employee {emp_code} not found"})
        
        issues = []
        
        # Check basic salary
        if not emp["basic"] or float(emp["basic"] or 0) == 0:
            issues.append("Basic salary is 0 or not set")
        
        # Check attendance
        att = conn.execute("""SELECT COUNT(*) as cnt FROM attendance
            WHERE emp_code=? AND strftime('%m',att_date)=? AND strftime('%Y',att_date)=?""",
            (emp_code, f"{m:02d}", str(y))).fetchone()
        
        # Check scheme
        scheme = None
        if emp["scheme_id"]:
            scheme = conn.execute("SELECT scheme_name FROM employee_schemes WHERE id=?",
                (emp["scheme_id"],)).fetchone()
        
        conn.close()
        
        return jsonify({
            "emp_code": emp_code,
            "emp_name": emp["emp_name"],
            "category": emp["category"],
            "basic": emp["basic"],
            "hra": emp["hra"],
            "special_allowance": emp["special_allowance"],
            "scheme": scheme["scheme_name"] if scheme else "None (manual settings)",
            "pf_applicable": emp["pf_applicable"],
            "esi_applicable": emp["esi_applicable"],
            "attendance_records": att["cnt"],
            "issues": issues,
            "status": "OK" if not issues else "Has issues"
        })
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e), "traceback": traceback.format_exc()})

@app.route("/payroll/preview", methods=["POST"])
@amgr
def payroll_preview():
    """Preview salary — uses stored record if available, else live calculation"""
    d = request.json
    emp_code = d.get("emp_code","")
    month    = int(d.get("month", date.today().month))
    year     = int(d.get("year",  date.today().year))

    conn = get_db()
    # Use stored salary_record if it exists — ensures preview matches main table
    stored = conn.execute("""SELECT sr.*, e.emp_name, e.department, e.designation,
        e.category, e.bank_account, e.bank_name, e.ifsc, e.uan_number,
        e.pf_number, e.esic_number, e.pan
        FROM salary_records sr JOIN employees e ON sr.emp_code=e.emp_code
        WHERE sr.emp_code=? AND sr.month=? AND sr.year=?""",
        (emp_code, month, year)).fetchone()
    conn.close()

    if stored:
        result = dict(stored)
        return jsonify({"success":True,"data":result,"source":"stored"})

    # No stored record — fall back to live calculation (preview only)
    result = calc_salary_emp(emp_code, month, year, preview=True)
    if result:
        return jsonify({"success":True,"data":result,"source":"live"})
    return jsonify({"success":False,"error":"Employee not found or calculation failed"})


@app.route("/payroll/unlock", methods=["POST"])
@amgr
def unlock_salary():
    """Unlock salary records for a specific month/year so they can be reprocessed"""
    d = request.json
    month = int(d.get("month", date.today().month))
    year  = int(d.get("year",  date.today().year))
    emp_code = d.get("emp_code","")
    conn = get_db()
    try:
        if emp_code:
            conn.execute("UPDATE salary_records SET locked=0 WHERE emp_code=? AND month=? AND year=?",
                (emp_code, month, year))
            msg = f"Unlocked {emp_code} for {month}/{year}"
        else:
            conn.execute("UPDATE salary_records SET locked=0 WHERE month=? AND year=?", (month, year))
            # Also remove from payroll_locks so Process All works again
            conn.execute("DELETE FROM payroll_locks WHERE month=? AND year=?", (month, year))
            msg = f"Unlocked all records for {month}/{year} — payroll can be re-processed."
        count = conn.total_changes
        conn.commit(); conn.close()
        return jsonify({"success":True,"message":msg,"count":count})
    except Exception as e:
        conn.close(); return jsonify({"success":False,"error":str(e)})

@app.route("/payroll/lock-status")
@amgr
def salary_lock_status():
    """Check lock status for a month"""
    month = int(request.args.get("month", date.today().month))
    year  = int(request.args.get("year",  date.today().year))
    conn = get_db()
    total  = conn.execute("SELECT COUNT(*) as c FROM salary_records WHERE month=? AND year=?", (month,year)).fetchone()["c"]
    locked = conn.execute("SELECT COUNT(*) as c FROM salary_records WHERE month=? AND year=? AND locked=1", (month,year)).fetchone()["c"]
    conn.close()
    return jsonify({"success":True,"total":total,"locked":locked,"unlocked":total-locked})

@app.route("/payroll/process-all", methods=["POST"])
@amgr
def payroll_process_all():
    """Process salary OR deductions for all/selected employees"""
    d = request.json
    month    = int(d.get("month", date.today().month))
    year     = int(d.get("year",  date.today().year))
    dept     = d.get("dept","")
    cat      = d.get("category","")
    emp_list = d.get("emp_codes",[])
    mode     = d.get("mode","salary")  # "salary" or "deduction"

    conn = get_db()

    # Lock check — applies to both modes
    lock_chk = conn.execute("SELECT locked_by FROM payroll_locks WHERE month=? AND year=?", (month, year)).fetchone()
    if lock_chk:
        conn.close()
        return jsonify({"success": False,
            "error": f"Payroll for {MONTHS[month-1]} {year} is LOCKED by {lock_chk['locked_by']}. Unlock first to re-process."})

    scheme   = d.get("scheme","")
    # Include employees who were Active during that month:
    # Either currently Active, OR left but last_working_day is within/after that month's start
    import calendar as _pc
    _month_start = f"{year}-{month:02d}-01"
    sql = """SELECT emp_code FROM employees WHERE (
        status='Active'
        OR (status IN ('Resigned','Terminated','Inactive','Left')
            AND (last_working_day IS NULL OR last_working_day >= ?))
    )"""
    params = [_month_start]
    if dept:     sql += " AND department=?"; params.append(dept)
    if cat:      sql += " AND category=?";   params.append(cat)
    if scheme:
        conn2 = get_db()
        sc = conn2.execute("SELECT id FROM employee_schemes WHERE scheme_name=? AND is_active=1",(scheme,)).fetchone()
        conn2.close()
        if sc: sql += " AND scheme_id=?"; params.append(sc["id"])
    if emp_list: sql += f" AND emp_code IN ({','.join('?'*len(emp_list))})"; params.extend(emp_list)
    emps = conn.execute(sql, params).fetchall()

    if mode == "deduction":
        # Deduction-only mode: apply loan/advance/canteen/fine deductions
        # to already-processed salary records WITHOUT recalculating salary
        processed=0; failed=0; errors=[]
        for e in emps:
            ec = e["emp_code"]
            try:
                sr = conn.execute(
                    "SELECT * FROM salary_records WHERE emp_code=? AND month=? AND year=?",
                    (ec, month, year)).fetchone()
                if not sr:
                    failed += 1
                    errors.append({"emp_code": ec, "error": "No salary record — run Process Salary first"})
                    continue

                # Use SAME query logic as calc_salary_emp
                deds = conn.execute("""SELECT * FROM custom_deductions
                    WHERE emp_code=? AND status='Active'
                    AND (start_year < ? OR (start_year=? AND start_month<=?))""",
                    (ec, year, year, month)).fetchall()

                advance_ded=0.0; _loan_ded=0.0; _canteen_ded=0.0; _fine_ded=0.0
                ded_ids_to_update = []

                for ded in deds:
                    ded_mode = ded["deduction_mode"] if "deduction_mode" in ded.keys() else "loan"
                    monthly   = float(ded["monthly_amount"] or 0)
                    remaining = float(ded["total_amount"] or 0) - float(ded["amount_deducted"] or 0)

                    if ded_mode == "monthly":
                        em = ded["end_month"] or ded["start_month"]
                        ey = ded["end_year"]  or ded["start_year"]
                        cur_val   = year * 100 + month
                        start_val = (ded["start_year"] or year)*100 + (ded["start_month"] or month)
                        end_val   = (ey or year)*100 + (em or month)
                        if not (start_val <= cur_val <= end_val):
                            continue

                    if remaining <= 0:
                        conn.execute("UPDATE custom_deductions SET status='Completed' WHERE id=?", (ded["id"],))
                        continue

                    this_month = min(monthly, remaining)
                    advance_ded += this_month
                    ded_type_lower = (ded["deduction_type"] or "").lower()
                    if "loan" in ded_type_lower or "advance" in ded_type_lower:
                        _loan_ded += this_month
                    elif "fine" in ded_type_lower or "penalty" in ded_type_lower:
                        _fine_ded += this_month
                    elif "uniform" in ded_type_lower or "other" in ded_type_lower or \
                         "canteen" in ded_type_lower or "food" in ded_type_lower or "lunch" in ded_type_lower:
                        _canteen_ded += this_month
                    else:
                        _loan_ded += this_month
                    ded_ids_to_update.append((round(this_month, 2), ded["id"]))

                # Recalculate totals
                gross = float(sr["gross"] or 0)
                pf    = float(sr["pf"] or 0)
                esi   = float(sr["esi"] or 0)
                pt    = float(sr["pt"] or 0)
                lwf   = float(sr["lwf"] or 0)
                tds   = float(sr["tds"] or 0)
                statutory_ded = round(pf + esi + pt + lwf + tds, 2)
                net_after_statutory = round(gross - statutory_ded, 2)

                # Skip custom deductions if they exceed net after statutory
                skip_custom = False
                skip_reason = ""
                if advance_ded > net_after_statutory:
                    skip_custom = True
                    skip_reason = (f"Custom deductions (₹{advance_ded:,.0f}) exceed net after statutory "
                                   f"(₹{net_after_statutory:,.0f}) — Loan/Uniform/Penalty/Lunch skipped")
                    advance_ded = 0.0; _loan_ded = 0.0; _canteen_ded = 0.0; _fine_ded = 0.0
                    # Revert amount_deducted updates
                    ded_ids_to_update = []

                total_ded = round(statutory_ded + advance_ded, 2)
                net_sal   = round(gross - total_ded, 2)

                conn.execute("""UPDATE salary_records SET
                    advance_deduction=?, loan_deduction=?, canteen_deduction=?,
                    fine_deduction=?, total_deductions=?, net_salary=?,
                    skip_deductions=?, skip_reason=?
                    WHERE emp_code=? AND month=? AND year=?""",
                    (round(advance_ded,2), round(_loan_ded,2), round(_canteen_ded,2),
                     round(_fine_ded,2), total_ded, net_sal,
                     1 if skip_custom else 0, skip_reason,
                     ec, month, year))

                # Update amount_deducted in custom_deductions
                for amt, did in ded_ids_to_update:
                    conn.execute("UPDATE custom_deductions SET amount_deducted=amount_deducted+? WHERE id=?",
                                 (amt, did))
                processed += 1

            except Exception as ex:
                failed += 1
                errors.append({"emp_code": ec, "error": str(ex)[:100]})

        conn.commit(); conn.close()
        return jsonify({"success":True, "processed":processed, "failed":failed,
            "errors":errors[:10],
            "message":f"✅ Deductions applied for {processed} employees — {MONTHS[month-1]} {year}. {failed} failed."})

    conn.close()
    processed=0; failed=0; errors=[]
    for e in emps:
        try:
            result = calc_salary_emp(e["emp_code"], month, year)
            if result: processed += 1
            else:
                failed += 1
                errors.append({"emp_code": e["emp_code"], "error": "Calculation returned None — check Basic salary and attendance"})
        except Exception as ex:
            import traceback
            failed += 1
            err_detail = str(ex)
            print(f"[PAYROLL FAIL] {e['emp_code']}: {err_detail}")
            traceback.print_exc()
            errors.append({"emp_code": e["emp_code"], "error": err_detail[:100]})

    return jsonify({"success":True,"processed":processed,"failed":failed,
                   "errors":errors[:10],
                   "message":f"✅ {processed} processed, {failed} failed for {MONTHS[month-1]} {year}"})


# ════════════════════════════════════════════════════════════
# OT PROCESS — Separate from Salary
# ════════════════════════════════════════════════════════════


# ─── PAYROLL LOCK ─────────────────────────────────────────

@app.route("/payroll/lock-status/<int:m>/<int:y>")
@amgr
def payroll_lock_status(m, y):
    conn = get_db()
    lock = conn.execute("SELECT * FROM payroll_locks WHERE month=? AND year=?", (m, y)).fetchone()
    conn.close()
    if lock:
        return jsonify({"locked": True, "locked_by": lock["locked_by"],
                        "locked_at": lock["locked_at"]})
    return jsonify({"locked": False})

@app.route("/payroll/lock", methods=["POST"])
@amgr
def payroll_lock():
    d = request.json or {}
    m = int(d.get("month", 0))
    y = int(d.get("year", 0))
    if not m or not y:
        return jsonify({"success": False, "error": "Month and year required"})
    username = session.get("user", "admin")
    conn = get_db()
    try:
        existing = conn.execute("SELECT id FROM payroll_locks WHERE month=? AND year=?", (m, y)).fetchone()
        if existing:
            return jsonify({"success": False, "error": f"Payroll for {MONTHS[m-1]} {y} is already locked."})
        conn.execute("INSERT INTO payroll_locks (month, year, locked_by) VALUES (?,?,?)",
                     (m, y, username))
        conn.commit()
        return jsonify({"success": True, "message": f"Payroll for {MONTHS[m-1]} {y} locked successfully."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
    finally:
        conn.close()

@app.route("/payroll/ot-lock", methods=["POST"])
@amgr
def ot_payment_lock():
    """Lock/unlock OT Payment for a month — separate from payroll lock"""
    d = request.json or {}
    m = int(d.get("month", 0)); y = int(d.get("year", 0))
    action = d.get("action", "lock")
    if not m or not y:
        return jsonify({"success": False, "error": "Month and year required"})
    username = session.get("user", "admin")
    conn = get_db()
    try:
        # Ensure table exists
        conn.execute("""CREATE TABLE IF NOT EXISTS ot_payment_locks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            month INTEGER NOT NULL, year INTEGER NOT NULL,
            locked_by TEXT NOT NULL,
            locked_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(month, year))""")
        if action == "unlock":
            conn.execute("DELETE FROM ot_payment_locks WHERE month=? AND year=?", (m, y))
            conn.commit()
            return jsonify({"success": True, "message": f"OT Payment for {MONTHS[m-1]} {y} unlocked."})
        existing = conn.execute("SELECT id FROM ot_payment_locks WHERE month=? AND year=?", (m, y)).fetchone()
        if existing:
            return jsonify({"success": False, "error": f"OT Payment for {MONTHS[m-1]} {y} is already locked."})
        conn.execute("INSERT INTO ot_payment_locks (month, year, locked_by) VALUES (?,?,?)", (m, y, username))
        conn.commit()
        return jsonify({"success": True, "message": f"OT Payment for {MONTHS[m-1]} {y} locked."})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
    finally:
        conn.close()


@app.route("/payroll/summary")
@amgr
def payroll_summary():
    """Payroll summary for a month"""
    m = int(request.args.get("month", date.today().month))
    y = int(request.args.get("year",  date.today().year))
    conn = get_db()
    # Backfill actual_gross for records that were processed before this column was added
    try:
        conn.execute("""UPDATE salary_records SET actual_gross = (
            SELECT COALESCE(e.basic,0)+COALESCE(e.hra,0)+COALESCE(e.special_allowance,0)
            FROM employees e WHERE e.emp_code=salary_records.emp_code)
            WHERE month=? AND year=? AND (actual_gross IS NULL OR actual_gross=0)""", (m,y))
        conn.commit()
    except: pass

    records = conn.execute("""SELECT sr.*, e.emp_name, e.department, e.category, e.bank_account,
        COALESCE(es.scheme_name,'') as scheme_name
        FROM salary_records sr JOIN employees e ON sr.emp_code=e.emp_code
        LEFT JOIN employee_schemes es ON e.scheme_id=es.id
        WHERE sr.month=? AND sr.year=?
        ORDER BY e.department, e.emp_name""", (m,y)).fetchall()
    totals  = conn.execute("""SELECT
        COUNT(*) as count,
        SUM(gross) as total_gross, SUM(net_salary) as total_net,
        SUM(pf) as total_pf, SUM(employer_pf) as total_epf,
        SUM(esi) as total_esi, SUM(employer_esi) as total_eesi,
        SUM(pt) as total_pt, SUM(tds) as total_tds,
        SUM(ot_amount) as total_ot, SUM(bonus) as total_bonus,
        SUM(COALESCE(loan_deduction,0)) as total_loan,
        SUM(COALESCE(canteen_deduction,0)) as total_canteen,
        SUM(COALESCE(fine_deduction,0)) as total_fine,
        SUM(COALESCE(actual_gross,0)) as total_actual_gross,
        SUM(total_deductions) as total_all_deductions
        FROM salary_records WHERE month=? AND year=?""", (m,y)).fetchone()
    dept_summary = conn.execute("""SELECT e.department,
        COUNT(*) as count, SUM(sr.net_salary) as total
        FROM salary_records sr JOIN employees e ON sr.emp_code=e.emp_code
        WHERE sr.month=? AND sr.year=? GROUP BY e.department""", (m,y)).fetchall()
    # Employees with skipped deductions — show warning
    skipped_deduction_emps = conn.execute("""SELECT sr.emp_code, e.emp_name, sr.skip_reason
        FROM salary_records sr JOIN employees e ON sr.emp_code=e.emp_code
        WHERE sr.month=? AND sr.year=? AND sr.skip_deductions=1""", (m,y)).fetchall()
    # ── Warning: employees with basic salary = 0 ──────────────
    zero_basic_emps = conn.execute(
        """SELECT emp_code, emp_name, department FROM employees
           WHERE status='Active' AND (basic IS NULL OR basic = 0)
           ORDER BY department, emp_name""").fetchall()
    schemes = conn.execute("SELECT scheme_name FROM employee_schemes WHERE is_active=1 ORDER BY scheme_name").fetchall()
    conn.close()
    user_perms = session.get("permissions", [])
    user_role  = session.get("role", "")
    can_process  = user_role == "admin" or "payroll_process" in user_perms or "payroll_process_edit" in user_perms
    can_mark_paid= user_role == "admin" or "payroll_mark_paid" in user_perms or "payroll_mark_paid_edit" in user_perms
    return render_template("payroll_summary.html",
        records=[dict(r) for r in records],
        totals=dict(totals) if totals else {},
        dept_summary=[dict(d) for d in dept_summary],
        zero_basic_emps=[dict(z) for z in zero_basic_emps],
        skipped_deduction_emps=[dict(s) for s in skipped_deduction_emps],
        schemes=[s["scheme_name"] for s in schemes],
        month=m, year=y, month_name=MONTHS[m-1],
        months=MONTHS,
        today_str=date.today().strftime("%Y-%m-%d"),
        can_process=can_process, can_mark_paid=can_mark_paid)

@app.route("/payroll/bonus/add", methods=["POST"])
@amgr
def payroll_bonus_add():
    d = request.json; conn = get_db()
    try:
        emp_codes = d.get("emp_codes",[]) or [d.get("emp_code","")]
        month = int(d.get("month", date.today().month))
        year  = int(d.get("year",  date.today().year))
        for ec in emp_codes:
            if not ec: continue
            conn.execute("""INSERT INTO payroll_bonus
                (emp_code,bonus_type,amount,month,year,remarks,created_by,created_on)
                VALUES (?,?,?,?,?,?,?,datetime('now'))""",
                (ec, d.get("bonus_type","festival"), float(d.get("amount",0)),
                 month, year, d.get("remarks",""), session.get("name","HR")))
        conn.commit()
        return jsonify({"success":True,"added":len(emp_codes)})
    except Exception as e: return jsonify({"success":False,"error":str(e)})
    finally: conn.close()

@app.route("/payroll/mark-paid", methods=["POST"])
@amgr
def payroll_mark_paid():
    d = request.json; conn = get_db()
    try:
        month = int(d.get("month", date.today().month))
        year  = int(d.get("year",  date.today().year))
        emp_codes = d.get("emp_codes",[])
        payment_date = d.get("payment_date", date.today().strftime("%Y-%m-%d"))
        payment_mode = d.get("payment_mode","Bank Transfer")
        payment_ref  = d.get("payment_ref","")

        if emp_codes:
            for ec in emp_codes:
                conn.execute("""UPDATE salary_records SET
                    payment_status='Paid', payment_date=?, payment_mode=?, payment_ref=?
                    WHERE emp_code=? AND month=? AND year=?""",
                    (payment_date, payment_mode, payment_ref, ec, month, year))
                # Insert payment record
                rec = conn.execute("SELECT net_salary FROM salary_records WHERE emp_code=? AND month=? AND year=?",
                    (ec,month,year)).fetchone()
                if rec:
                    conn.execute("""INSERT OR REPLACE INTO payroll_payments
                        (emp_code,month,year,net_amount,payment_mode,payment_date,
                         payment_ref,status,processed_by,processed_on)
                        VALUES (?,?,?,?,?,?,?,'Paid',?,datetime('now'))""",
                        (ec,month,year,rec["net_salary"],payment_mode,payment_date,
                         payment_ref,session.get("name","HR")))
        else:
            # Mark all
            conn.execute("""UPDATE salary_records SET
                payment_status='Paid',payment_date=?,payment_mode=?,payment_ref=?
                WHERE month=? AND year=?""",
                (payment_date,payment_mode,payment_ref,month,year))
        conn.commit()
        return jsonify({"success":True})
    except Exception as e: return jsonify({"success":False,"error":str(e)})
    finally: conn.close()

@app.route("/payroll/bank-file/<int:m>/<int:y>")
@amgr
def payroll_bank_file(m,y):
    """Generate bank transfer file"""
    import openpyxl
    conn = get_db()
    records = conn.execute("""SELECT sr.emp_code, e.emp_name, e.bank_account, e.bank_name,
        e.ifsc, sr.net_salary, sr.payment_status
        FROM salary_records sr JOIN employees e ON sr.emp_code=e.emp_code
        WHERE sr.month=? AND sr.year=? ORDER BY e.emp_name""", (m,y)).fetchall()
    conn.close()

    wb = openpyxl.Workbook(); ws = wb.active
    ws.title = f"Bank Transfer {MONTHS[m-1]} {y}"
    from openpyxl.styles import Font, PatternFill, Alignment
    hdrs = ["Emp Code","Employee Name","Bank Account","Bank Name","IFSC Code",
            "Net Salary","Payment Status"]
    ws.append(hdrs)
    for cell in ws[1]:
        cell.font = Font(bold=True,color="FFFFFF")
        cell.fill = PatternFill("solid",fgColor="0052CC")
        cell.alignment = Alignment(horizontal="center")
    total = 0
    for r in records:
        ws.append([r["emp_code"],r["emp_name"],r["bank_account"] or "",
                   r["bank_name"] or "",r["ifsc"] or "",
                   r["net_salary"] or 0, r["payment_status"] or "Pending"])
        total += (r["net_salary"] or 0)
    ws.append(["","","","","TOTAL",total,""])
    ws.cell(ws.max_row,5).font = Font(bold=True)
    ws.cell(ws.max_row,6).font = Font(bold=True)
    from openpyxl.utils import get_column_letter
    for i,w in enumerate([10,25,18,18,14,12,12],1):
        ws.column_dimensions[get_column_letter(i)].width = w
    return xlresp(wb, f"BankFile_{MONTHS[m-1]}_{y}.xlsx")

@app.route("/payroll/pt-slabs", methods=["GET","POST"])
@amgr
def payroll_pt_slabs():
    conn = get_db()
    if request.method == "POST":
        d = request.json
        if d.get("id"):
            conn.execute("""UPDATE pt_slabs SET salary_from=?,salary_to=?,pt_amount=?
                WHERE id=?""", (d["salary_from"],d["salary_to"],d["pt_amount"],d["id"]))
        else:
            conn.execute("""INSERT INTO pt_slabs (state,salary_from,salary_to,pt_amount,is_active)
                VALUES (?,?,?,?,1)""",
                (d.get("state","Madhya Pradesh"),d["salary_from"],d["salary_to"],d["pt_amount"]))
        conn.commit(); conn.close()
        return jsonify({"success":True})
    slabs = conn.execute("SELECT * FROM pt_slabs ORDER BY salary_from").fetchall()
    conn.close()
    return jsonify([dict(s) for s in slabs])

@app.route("/payroll/reports/pf/<int:m>/<int:y>")
@amgr
def payroll_report_pf(m,y):
    """PF Report Export"""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    conn = get_db()
    records = conn.execute("""SELECT sr.emp_code, e.emp_name, e.department,
        sr.basic_earned, sr.pf, sr.employer_pf
        FROM salary_records sr JOIN employees e ON sr.emp_code=e.emp_code
        WHERE sr.month=? AND sr.year=? AND (sr.pf>0 OR sr.employer_pf>0)
        ORDER BY e.emp_name""", (m,y)).fetchall()
    conn.close()
    wb = openpyxl.Workbook(); ws = wb.active
    ws.title = "PF Report"
    ws.merge_cells("A1:F1")
    ws["A1"] = f"PF Report — {MONTHS[m-1]} {y} — {COMPANY}"
    ws["A1"].font = Font(bold=True,size=12,color="FFFFFF")
    ws["A1"].fill = PatternFill("solid",fgColor="0052CC")
    ws["A1"].alignment = Alignment(horizontal="center")
    hdrs = ["Emp Code","Name","Department","Basic Earned","Employee PF (12%)","Employer PF (12%)"]
    ws.append(hdrs)
    for cell in ws[2]:
        cell.font = Font(bold=True,color="FFFFFF")
        cell.fill = PatternFill("solid",fgColor="1E3A5F")
    epf=0;erpf=0
    for r in records:
        ws.append([r["emp_code"],r["emp_name"],r["department"],
                   r["basic_earned"],r["pf"],r["employer_pf"]])
        epf+=r["pf"] or 0; erpf+=r["employer_pf"] or 0
    ws.append(["","","TOTAL","",epf,erpf])
    from openpyxl.utils import get_column_letter
    for i,w in enumerate([10,25,18,14,16,16],1):
        ws.column_dimensions[get_column_letter(i)].width = w
    return xlresp(wb, f"PF_Report_{MONTHS[m-1]}_{y}.xlsx")

@app.route("/payroll/reports/esic/<int:m>/<int:y>")
@amgr
def payroll_report_esic(m,y):
    """ESIC Report Export"""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    conn = get_db()
    records = conn.execute("""SELECT sr.emp_code, e.emp_name, e.department,
        sr.gross, sr.esi, sr.employer_esi
        FROM salary_records sr JOIN employees e ON sr.emp_code=e.emp_code
        WHERE sr.month=? AND sr.year=? AND (sr.esi>0 OR sr.employer_esi>0)
        ORDER BY e.emp_name""", (m,y)).fetchall()
    conn.close()
    wb = openpyxl.Workbook(); ws = wb.active
    ws.title = "ESIC Report"
    ws.merge_cells("A1:F1")
    ws["A1"] = f"ESIC Report — {MONTHS[m-1]} {y} — {COMPANY}"
    ws["A1"].font = Font(bold=True,size=12,color="FFFFFF")
    ws["A1"].fill = PatternFill("solid",fgColor="059669")
    ws["A1"].alignment = Alignment(horizontal="center")
    hdrs = ["Emp Code","Name","Department","Gross Salary","Employee ESIC (0.75%)","Employer ESIC (3.25%)"]
    ws.append(hdrs)
    for cell in ws[2]:
        cell.font = Font(bold=True,color="FFFFFF")
        cell.fill = PatternFill("solid",fgColor="1E3A5F")
    e_esi=0;er_esi=0
    for r in records:
        ws.append([r["emp_code"],r["emp_name"],r["department"],
                   r["gross"],r["esi"],r["employer_esi"]])
        e_esi+=r["esi"] or 0; er_esi+=r["employer_esi"] or 0
    ws.append(["","","TOTAL","",e_esi,er_esi])
    from openpyxl.utils import get_column_letter
    for i,w in enumerate([10,25,18,14,18,18],1):
        ws.column_dimensions[get_column_letter(i)].width = w
    return xlresp(wb, f"ESIC_Report_{MONTHS[m-1]}_{y}.xlsx")

@app.route("/salary")
@amgr
def salary():
    m   = int(request.args.get("month",date.today().month))
    y   = int(request.args.get("year", date.today().year))
    cat = request.args.get("cat","")
    conn = get_db()
    sql = """SELECT s.*,e.emp_name,e.department,e.category
        FROM salary_records s JOIN employees e ON s.emp_code=e.emp_code
        WHERE s.month=? AND s.year=?"""
    params = [m,y]
    if cat == "Staff":    sql += " AND e.category='Staff'"
    elif cat == "NonStaff": sql += " AND e.category!='Staff'"
    sql += " ORDER BY e.category,e.emp_name"
    recs = conn.execute(sql, params).fetchall()
    conn.close()
    return render_template("salary.html",records=recs,month=m,year=y,months=MONTHS,month_name=MONTHS[m-1],cat_filter=cat)

@app.route("/salary/calculate",methods=["POST"])
@amgr
def calc_all():
    data  = request.json
    m     = int(data.get("month", date.today().month))
    y     = int(data.get("year",  date.today().year))
    emp_code = str(data.get("emp_code","") or "").strip()  # single emp if specified

    # Check if this month is locked
    conn_lock = get_db()
    lock = conn_lock.execute("SELECT locked_by, locked_at FROM payroll_locks WHERE month=? AND year=?", (m, y)).fetchone()
    conn_lock.close()
    if lock:
        return jsonify({"success": False,
                        "error": f"Payroll for {MONTHS[m-1]} {y} is LOCKED by {lock['locked_by']}. Unlock first to re-process."})

    conn  = get_db()
    if emp_code:
        emps = conn.execute("SELECT emp_code FROM employees WHERE emp_code=?", (emp_code,)).fetchall()
    else:
        emps = conn.execute("SELECT emp_code FROM employees WHERE status='Active'").fetchall()
    conn.close()
    
    count = 0; errors = 0
    for e in emps:
        try:
            result = calc_salary_emp(e["emp_code"], m, y)
            if result is not None: count += 1
        except Exception as ex:
            print(f"[SALARY ERR] {e['emp_code']}: {ex}")
            errors += 1
    
    return jsonify({"success":True, "count":count, "errors":errors, "total":len(emps)})

@app.route("/salary/increment",methods=["POST"])
@amgr
def do_increment():
    """
    Salary Increment Logic:
    - Increment applies on ACTUAL GROSS (Basic + HRA + Special)
    - New Gross = Old Gross + Increment Amount (or % of old gross)
    - Split: 60% of New Gross → Basic, 40% → HRA
    - Special Allowance stays same (no change)
    - NO impact on past salary records or past OT (salary_records locked)
    - Only employee master is updated (future months use new salary)
    """
    d=request.json; conn=get_db()
    try:
        emp=conn.execute("SELECT * FROM employees WHERE emp_code=?",(d["emp_code"],)).fetchone()
        if not emp: return jsonify({"success":False,"error":"Employee not found"})

        old_basic   = float(emp["basic"] or 0)
        old_hra     = float(emp["hra"] or 0)
        old_special = float(emp["special_allowance"] or 0)
        old_gross   = old_basic + old_hra + old_special

        inc_type  = d.get("increment_type","amount")
        inc_value = float(d.get("increment_value",0) or 0)

        # Calculate increment amount on ACTUAL GROSS
        if inc_type == "percent":
            inc_amount = round(old_gross * inc_value / 100, 2)
        else:
            inc_amount = inc_value

        new_gross   = round(old_gross + inc_amount, 2)

        # Split: 60% Basic, 40% HRA (Special unchanged)
        distributable = new_gross - old_special
        new_basic   = round(distributable * 0.60, 2)
        new_hra     = round(distributable * 0.40, 2)
        new_special = old_special  # unchanged

        # Apply minimum basic if set in payroll settings
        try:
            ps = conn.execute("SELECT min_basic FROM payroll_settings WHERE id=1").fetchone()
            min_basic = float(ps["min_basic"] or 0) if ps else 0
            if min_basic > 0 and new_basic < min_basic:
                # Basic can't go below minimum — shift difference to HRA
                diff = min_basic - new_basic
                new_basic = min_basic
                new_hra   = max(0, round(new_hra - diff, 2))
        except: pass

        # Verify gross is preserved
        # (minor rounding: new_basic+new_hra+new_special ≈ new_gross)

        # Update ONLY employee master — NO past salary/OT records touched
        conn.execute("UPDATE employees SET basic=?,hra=?,special_allowance=? WHERE emp_code=?",
                     (new_basic,new_hra,new_special,d["emp_code"]))
        conn.execute("""INSERT INTO increments
            (emp_code,effective_date,old_basic,new_basic,reason,done_on)
            VALUES (?,?,?,?,?,?)""",
            (d["emp_code"],
             d.get("effective_date",date.today().strftime("%Y-%m-%d")),
             old_basic,new_basic,
             d.get("reason",""),
             datetime.now().strftime("%Y-%m-%d")))
        conn.commit()
        return jsonify({
            "success":True,
            "old_gross":old_gross,  "new_gross":new_gross,
            "old_basic":old_basic,  "new_basic":new_basic,
            "old_hra":old_hra,      "new_hra":new_hra,
            "new_special":new_special,
            "increment_amount":inc_amount
        })
    except Exception as e: return jsonify({"success":False,"error":str(e)})
    finally: conn.close()


@app.route("/salary/increment/bulk", methods=["POST"])
@amgr
def do_increment_bulk():
    """Bulk salary increment from Excel upload"""
    if "file" not in request.files:
        return jsonify({"success":False,"error":"No file"})
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(request.files["file"].read()))
        ws = wb.active
        hdrs = [str(c.value or "").strip().lower().replace(" ","_") for c in ws[1]]
        conn = get_db()
        done=0; errors=[]
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not any(row): continue
            d = dict(zip(hdrs, row))
            ec = str(d.get("emp_code","") or "").strip()
            if not ec: continue
            emp = conn.execute("SELECT * FROM employees WHERE emp_code=?", (ec,)).fetchone()
            if not emp: errors.append(f"{ec}: not found"); continue
            old_basic = float(emp["basic"] or 0)
            old_hra   = float(emp["hra"] or 0)
            old_sp    = float(emp["special_allowance"] or 0)
            old_gross = old_basic + old_hra + old_sp
            inc_type  = str(d.get("increment_type","amount") or "amount").strip().lower()
            inc_value = float(d.get("increment_value",0) or d.get("amount",0) or 0)
            if inc_type == "percent":
                inc_amount = round(old_gross * inc_value / 100, 2)
            else:
                inc_amount = inc_value
            new_gross    = round(old_gross + inc_amount, 2)
            distributable = new_gross - old_sp
            new_basic    = round(distributable * 0.60, 2)
            new_hra      = round(distributable * 0.40, 2)
            eff_date     = str(d.get("effective_date","") or date.today().strftime("%Y-%m-%d"))[:10]
            reason       = str(d.get("reason","") or "Bulk increment")
            conn.execute("UPDATE employees SET basic=?,hra=? WHERE emp_code=?", (new_basic,new_hra,ec))
            conn.execute("INSERT INTO increments (emp_code,effective_date,old_basic,new_basic,reason,done_on) VALUES (?,?,?,?,?,?)",
                (ec,eff_date,old_basic,new_basic,reason,datetime.now().strftime("%Y-%m-%d")))
            done+=1
        conn.commit(); conn.close()
        return jsonify({"success":True,"done":done,"errors":errors[:10]})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})


@app.route("/salary/increment/template")
@amgr
def increment_template():
    """Download bulk increment Excel template"""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    wb = openpyxl.Workbook(); ws = wb.active
    ws.title = "Bulk Increment"
    hdrs = ["emp_code","increment_type","increment_value","effective_date","reason"]
    notes = ["Employee Code","amount OR percent","Amount(₹) or % value","YYYY-MM-DD","Reason"]
    for ci,(h,n) in enumerate(zip(hdrs,notes),1):
        c = ws.cell(1,ci,h)
        c.font=Font(bold=True,color="FFFFFF"); c.fill=PatternFill("solid",fgColor="0052CC")
        c.alignment=Alignment(horizontal="center")
        ws.cell(2,ci,n).font=Font(italic=True,color="888888",size=9)
    samples=[["1001","percent","10","2026-04-01","Annual increment"],
             ["1002","amount","5000","2026-04-01","Special increment"]]
    for ri,row in enumerate(samples,3):
        for ci,v in enumerate(row,1): ws.cell(ri,ci,v)
    for i,w in enumerate([14,16,16,16,24],1):
        ws.column_dimensions[__import__("openpyxl").utils.get_column_letter(i)].width=w
    out=io.BytesIO(); wb.save(out); out.seek(0)
    return send_file(out,mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    as_attachment=True,download_name="Bulk_Increment_Template.xlsx")

# ─── PAYSLIPS ─────────────────────────────────────────
@app.route("/payslip")
@amgr
def payslip():
    m   = int(request.args.get("month",date.today().month))
    y   = int(request.args.get("year", date.today().year))
    cat = request.args.get("cat","")
    conn = get_db()
    sql = """SELECT s.*,e.emp_name,e.department,e.designation,e.category,
        e.bank_account,e.bank_name,e.ifsc,e.pan,e.uan_number,e.pf_number,e.esic_number
        FROM salary_records s JOIN employees e ON s.emp_code=e.emp_code
        WHERE s.month=? AND s.year=?"""
    params = [m,y]
    if cat == "Staff":    sql += " AND e.category='Staff'"
    elif cat == "NonStaff": sql += " AND e.category!='Staff'"
    sql += " ORDER BY e.category,e.emp_name"
    recs = conn.execute(sql, params).fetchall()
    # Letter settings for header/footer/seal
    try:
        ls = conn.execute("SELECT header_filename, footer_filename, seal_filename FROM letter_settings WHERE id=1").fetchone()
        has_header = bool(ls and ls["header_filename"])
        has_footer = bool(ls and ls["footer_filename"])
        has_seal   = bool(ls and ls["seal_filename"])
    except:
        has_header = has_footer = has_seal = False
    conn.close()
    return render_template("payslip.html", records=recs, month=m, year=y, months=MONTHS,
        month_name=MONTHS[m-1], cat_filter=cat,
        has_header=has_header, has_footer=has_footer, has_seal=has_seal)

@app.route("/payslip/get/<emp_code>/<int:month>/<int:year>")
@lreq
def get_payslip(emp_code,month,year):
    if session.get("role")=="employee" and session.get("emp_id")!=emp_code:
        return jsonify({"error":"Unauthorized"})
    conn=get_db()
    s=conn.execute("""SELECT s.*,e.emp_name,e.department,e.designation,e.category,
        e.bank_account,e.bank_name,e.ifsc,e.pan,e.uan_number,e.pf_number,e.esic_number
        FROM salary_records s JOIN employees e ON s.emp_code=e.emp_code
        WHERE s.emp_code=? AND s.month=? AND s.year=?""",(emp_code,month,year)).fetchone()
    if not s: conn.close(); return jsonify({})
    s_dict = dict(s)
    # Generate and store payslip code if not already assigned
    if not s_dict.get("payslip_code"):
        emp_name = s_dict.get("emp_name", emp_code)
        pcode = get_next_letter_code("payslip", conn, emp_code, emp_name)
        try:
            conn.execute("UPDATE salary_records SET payslip_code=? WHERE emp_code=? AND month=? AND year=?",
                         (pcode, emp_code, month, year))
            conn.commit()
            s_dict["payslip_code"] = pcode
        except: pass
    else:
        pcode = s_dict["payslip_code"]
        # Ensure it's in document_log (for existing payslips)
        try:
            existing = conn.execute("SELECT id FROM document_log WHERE doc_code=?", (pcode,)).fetchone()
            if not existing:
                from datetime import datetime as _dtnow
                conn.execute("""INSERT OR IGNORE INTO document_log
                    (doc_code, doc_type, emp_code, emp_name, generated_on, generated_by)
                    VALUES (?,?,?,?,?,?)""",
                    (pcode, "payslip", emp_code, s_dict.get("emp_name", emp_code),
                     _dtnow.now().strftime("%Y-%m-%d %H:%M:%S"), session.get("name","System")))
                conn.commit()
        except: pass
    conn.close()
    return jsonify(s_dict)

@app.route("/my-payslip")
@lreq
def my_payslip():
    eid = session.get("emp_id","").strip() if session.get("emp_id") else ""
    conn = get_db()
    if not eid:
        conn.close()
        return render_template("my_payslip.html", records=[], emp=None, months=MONTHS,
            error="No employee linked to your account. Contact HR.")
    recs = conn.execute("""SELECT s.*,e.emp_name,e.department,e.designation,e.bank_account,e.bank_name
        FROM salary_records s JOIN employees e ON s.emp_code=e.emp_code
        WHERE s.emp_code=? ORDER BY s.year DESC,s.month DESC LIMIT 12""",(eid,)).fetchall()
    emp = conn.execute("SELECT * FROM employees WHERE emp_code=?",(eid,)).fetchone()
    conn.close()
    return render_template("my_payslip.html",records=recs,emp=emp,months=MONTHS)

# ─── EXIT MANAGEMENT ──────────────────────────────────
@app.route("/exit")
@amgr
def exit_mgmt():
    conn=get_db()
    exited=conn.execute("SELECT * FROM employees WHERE status IN ('Resigned','Terminated') ORDER BY last_working_day DESC").fetchall()
    conn.close()
    return render_template("exit.html",exited=exited)

@app.route("/exit/process",methods=["POST"])
@amgr
def process_exit():
    d=request.json; conn=get_db()
    try:
        conn.execute("""UPDATE employees SET status=?,resignation_date=?,last_working_day=?,exit_reason=?
            WHERE emp_code=?""",
            (d.get("exit_type","Resigned"),d.get("resignation_date",""),
             d.get("last_working_day",""),d.get("reason",""),d["emp_code"]))
        conn.commit()
        # Calculate F&F
        emp=conn.execute("SELECT * FROM employees WHERE emp_code=?",(d["emp_code"],)).fetchone()
        lwd=datetime.strptime(d["last_working_day"],"%Y-%m-%d").date() if d.get("last_working_day") else date.today()
        doj=datetime.strptime(emp["date_of_joining"],"%Y-%m-%d").date() if emp["date_of_joining"] else date.today()
        years=round((lwd-doj).days/365.25,2)
        gratuity=0
        if years>=5:
            gratuity=round((emp["basic"]/26)*15*years,2)
        conn.close()
        return jsonify({"success":True,"years_of_service":years,"gratuity":gratuity})
    except Exception as e: conn.close(); return jsonify({"success":False,"error":str(e)})

@app.route("/exit/ff/<emp_code>")
@amgr
def full_final(emp_code):
    conn=get_db()
    emp=conn.execute("SELECT * FROM employees WHERE emp_code=?",(emp_code,)).fetchone()
    if not emp: conn.close(); return "Not found",404
    lwd=datetime.strptime(emp["last_working_day"],"%Y-%m-%d").date() if emp["last_working_day"] else date.today()
    doj=datetime.strptime(emp["date_of_joining"],"%Y-%m-%d").date() if emp["date_of_joining"] else date.today()
    years=round((lwd-doj).days/365.25,2)
    gratuity=round((emp["basic"]/26)*15*years,2) if years>=5 else 0
    # Pending salary for current month
    m,y=lwd.month,lwd.year
    pending_sal=conn.execute("SELECT net_salary FROM salary_records WHERE emp_code=? AND month=? AND year=?",
                             (emp_code,m,y)).fetchone()
    conn.close()
    return render_template("full_final.html",emp=dict(emp),years=years,gratuity=gratuity,
        pending_salary=pending_sal["net_salary"] if pending_sal else 0,months=MONTHS)


# ─── CTC MODULE ────────────────────────────────────────────────
@app.route("/payroll/ctc")
@amgr
def ctc_report():
    m          = int(request.args.get("month", date.today().month))
    y          = int(request.args.get("year",  date.today().year))
    dept       = request.args.get("dept","")
    cat        = request.args.get("cat","")
    emp_search = request.args.get("emp","")
    ctc_type   = request.args.get("ctc_type","earn")  # earn / actual
    conn = get_db()

    sql = f"""SELECT
        s.emp_code, e.emp_name, e.department, e.category, e.designation,
        COALESCE(s.basic_earned,0)     as basic_earned,
        COALESCE(s.hra_earned,0)       as hra_earned,
        COALESCE(s.special_earned,0)   as special_earned,
        COALESCE(s.ot_amount,0)        as ot_amount,
        COALESCE(s.gross,0)            as gross,
        COALESCE(s.pf,0)               as emp_pf,
        COALESCE(s.esi,0)              as emp_esi,
        COALESCE(s.pt,0)               as pt,
        COALESCE(s.tds,0)              as tds,
        COALESCE(s.total_deductions,0) as total_ded,
        COALESCE(s.net_salary,0)       as net_salary,
        COALESCE(s.employer_esi,0)     as employer_esi,
        COALESCE(e.basic,0)            as actual_basic,
        COALESCE(e.hra,0)              as actual_hra,
        COALESCE(e.special_allowance,0) as actual_special
    FROM salary_records s
    JOIN employees e ON s.emp_code=e.emp_code
    WHERE s.month=? AND s.year=?"""
    params = [m, y]
    if dept:   sql += " AND e.department=?"; params.append(dept)
    if cat:    sql += " AND e.category=?";   params.append(cat)
    if emp_search:
        sql += " AND (e.emp_code LIKE ? OR e.emp_name LIKE ?)"
        params += [f"%{emp_search}%", f"%{emp_search}%"]
    sql += " ORDER BY e.department, e.emp_name"
    rows = conn.execute(sql, params).fetchall()

    ctc_rows = []
    for r in rows:
        r = dict(r)
        if ctc_type == "earn":
            base_basic = r["basic_earned"]
            base_gross = r["gross"]
        else:
            base_basic = r["actual_basic"]
            base_gross = r["actual_basic"] + r["actual_hra"] + r["actual_special"]

        # Bonus = 8.33% of base_basic
        bonus_calc    = round(base_basic * 0.0833, 2)
        # Employer PF = 13% of base_basic
        employer_pf_calc = round(base_basic * 0.13, 2)
        # Employer ESIC = 3.25% of base_gross (if gross <= 21000)
        employer_esi_calc = round(base_gross * 0.0325, 2) if base_gross <= 21000 else 0
        # Gratuity monthly = basic / 26 * 15 / 12
        gratuity_m    = round((base_basic / 26 * 15) / 12, 2) if base_basic else 0

        total_ctc = round(base_gross + employer_pf_calc + employer_esi_calc + bonus_calc + gratuity_m, 2)

        r["base_basic"]        = base_basic
        r["base_gross"]        = base_gross
        r["bonus_calc"]        = bonus_calc
        r["employer_pf_calc"]  = employer_pf_calc
        r["employer_esi_calc"] = employer_esi_calc
        r["gratuity_m"]        = gratuity_m
        r["total_ctc"]         = total_ctc
        r["total_ctc_annual"]  = round(total_ctc * 12, 2)
        ctc_rows.append(r)

    depts = [d["department"] for d in conn.execute(
        "SELECT DISTINCT department FROM employees WHERE status='Active' AND department IS NOT NULL ORDER BY department").fetchall()]
    conn.close()

    return render_template("ctc_report.html",
        rows=ctc_rows, month=m, year=y, month_name=MONTHS[m-1],
        months=MONTHS, departments=depts,
        selected_dept=dept, selected_cat=cat, emp_search=emp_search,
        ctc_type=ctc_type,
        total_ctc_sum=sum(r["total_ctc"] for r in ctc_rows),
        today_year=date.today().year)


@app.route("/export/ctc")
@amgr
def export_ctc():
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    m   = int(request.args.get("month", date.today().month))
    y   = int(request.args.get("year",  date.today().year))
    dept       = request.args.get("dept","")
    cat        = request.args.get("cat","")
    emp_search = request.args.get("emp","")
    conn = get_db()

    sql = f"""SELECT s.emp_code, e.emp_name, e.department, e.category, e.designation,
        COALESCE(s.basic_earned,0) as basic_earned, COALESCE(s.hra_earned,0) as hra_earned,
        COALESCE(s.special_earned,0) as special_earned, COALESCE(s.ot_amount,0) as ot_amount,
        COALESCE(s.bonus,0) as bonus, COALESCE(s.gross,0) as gross,
        COALESCE(s.pf,0) as emp_pf, COALESCE(s.esi,0) as emp_esi,
        COALESCE(s.pt,0) as pt, COALESCE(s.tds,0) as tds,
        COALESCE(s.total_deductions,0) as total_ded, COALESCE(s.net_salary,0) as net_salary,
        COALESCE(s.employer_pf,0) as employer_pf, COALESCE(s.employer_esi,0) as employer_esi,
        COALESCE(e.basic,0) as actual_basic
    FROM salary_records s JOIN employees e ON s.emp_code=e.emp_code
    WHERE s.month=? AND s.year=?"""
    params = [m, y]
    if dept: sql += " AND e.department=?"; params.append(dept)
    if cat:  sql += " AND e.category=?";   params.append(cat)
    if emp_search:
        sql += " AND (e.emp_code LIKE ? OR e.emp_name LIKE ?)"
        params += [f"%{emp_search}%", f"%{emp_search}%"]
    sql += " ORDER BY e.department, e.emp_name"
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    wb = openpyxl.Workbook(); ws = wb.active
    ws.title = f"CTC {MONTHS[m-1]} {y}"

    hdr_fill = PatternFill("solid", fgColor="003580")
    sub_fill = PatternFill("solid", fgColor="1E3A5F")
    thin = Border(*[Side(style='thin')]*4)

    ws.merge_cells("A1:T1")
    ws["A1"] = f"VIJAYSHRI PACKAGING LTD. — CTC Report | {MONTHS[m-1]} {y}"
    ws["A1"].font = Font(bold=True, size=12, color="FFFFFF", name="Calibri")
    ws["A1"].fill = hdr_fill
    ws["A1"].alignment = Alignment(horizontal="center")

    hdrs = ["Code","Name","Department","Category","Designation",
            "Basic","Gross","OT Amount","Bonus(8.33%)",
            "Emp PF","Emp ESIC","PT","TDS","Total Deductions","Net Salary",
            "Employer PF(13%)","Employer ESIC(3.25%)","Gratuity(Monthly)","Bonus(Monthly)",
            "Total CTC (Monthly)","Total CTC (Annual)"]
    for ci,h in enumerate(hdrs,1):
        c = ws.cell(2, ci, h)
        c.font = Font(bold=True, color="FFFFFF", size=9, name="Calibri")
        c.fill = sub_fill
        c.alignment = Alignment(horizontal="center", wrap_text=True)
        c.border = thin

    row_num = 3
    ctc_type = request.args.get("ctc_type","earn")
    for r in rows:
        r = dict(r)
        if ctc_type == "earn":
            base_basic = r["basic_earned"]; base_gross = r["gross"]
        else:
            base_basic = r["actual_basic"]
            base_gross = r["actual_basic"]+r["actual_hra"]+r.get("actual_special",0)
        bonus_c   = round(base_basic * 0.0833, 2)
        epf_c     = round(base_basic * 0.13, 2)
        eesi_c    = round(base_gross * 0.0325, 2) if base_gross <= 21000 else 0
        grat_c    = round((base_basic/26*15)/12, 2) if base_basic else 0
        ctc_m     = round(base_gross + epf_c + eesi_c + bonus_c + grat_c, 2)
        row_data = [
            r["emp_code"], r["emp_name"], r["department"], r["category"], r["designation"],
            base_basic, base_gross, r["ot_amount"], bonus_c,
            r["emp_pf"], r["emp_esi"], r["pt"], r["tds"], r["total_ded"], r["net_salary"],
            epf_c, eesi_c, grat_c, bonus_c,
            ctc_m, round(ctc_m*12, 2)
        ]
        for ci, val in enumerate(row_data, 1):
            c = ws.cell(row_num, ci, val)
            c.font = Font(size=9, name="Calibri")
            c.border = thin
            if ci > 5: c.alignment = Alignment(horizontal="right")
        row_num += 1

    # Totals row
    for ci in range(6, 22):
        ws.cell(row_num, ci).value = sum(
            ws.cell(r, ci).value or 0 for r in range(3, row_num))
        ws.cell(row_num, ci).font = Font(bold=True, size=9, name="Calibri")
        ws.cell(row_num, ci).fill = PatternFill("solid", fgColor="F0F4F8")
        ws.cell(row_num, ci).border = thin
        ws.cell(row_num, ci).alignment = Alignment(horizontal="right")
    ws.cell(row_num, 1, "TOTAL").font = Font(bold=True, name="Calibri")

    widths = [10,25,16,12,16]+[11]*16
    for i,w in enumerate(widths,1):
        ws.column_dimensions[get_column_letter(i)].width = w
    ws.row_dimensions[2].height = 30

    return xlresp(wb, f"CTC_Report_{MONTHS[m-1]}_{y}.xlsx")

# ─── END CTC MODULE ────────────────────────────────────────────

# ─── REPORTS ──────────────────────────────────────────

# ─── YEARLY ATTENDANCE REPORT ──────────────────────────────
@app.route("/reports/yearly-attendance")
@amgr
def yearly_attendance_report():
    """Employee-wise Yearly Attendance Count — P, A, WOP, HP, Total OT, month-wise"""
    year     = int(request.args.get("year", date.today().year))
    dept     = request.args.get("dept","")
    cat      = request.args.get("cat","")
    scheme   = request.args.get("scheme","")
    conn     = get_db()

    extra = " AND e.status='Active'"
    params = []
    if dept:   extra += " AND e.department=?"; params.append(dept)
    if cat:    extra += " AND e.category=?";   params.append(cat)
    if scheme:
        _sc = conn.execute("SELECT id FROM employee_schemes WHERE scheme_name=? AND is_active=1",(scheme,)).fetchone()
        if _sc: extra += " AND e.scheme_id=?"; params.append(_sc["id"])

    emps = conn.execute(f"""
        SELECT e.emp_code, e.emp_name, e.department, e.category,
               COALESCE(es.scheme_name,'') as scheme_name
        FROM employees e
        LEFT JOIN employee_schemes es ON e.scheme_id=es.id
        WHERE 1=1{extra}
        ORDER BY e.department, e.emp_name
    """, params).fetchall()

    emp_codes = [e["emp_code"] for e in emps]
    if not emp_codes:
        conn.close()
        return render_template("yearly_attendance.html",
            year=year, emp_data=[], months=MONTHS,
            departments=[], categories=["Staff","Associate"],
            schemes=[], selected_dept=dept, selected_cat=cat, selected_scheme=scheme,
            years=list(range(2020, date.today().year+2)))

    # Fetch all attendance for year + these employees
    ph = ",".join("?"*len(emp_codes))
    att_rows = conn.execute(f"""
        SELECT emp_code,
               CAST(strftime('%m', att_date) AS INTEGER) as month,
               status, ot_minutes
        FROM attendance
        WHERE strftime('%Y', att_date)=?
          AND emp_code IN ({ph})
    """, [str(year)] + emp_codes).fetchall()

    # Build month-wise counts per employee
    from collections import defaultdict
    # Structure: emp_monthly[emp_code][month] = {P,A,WOP,HP,OT_min}
    emp_monthly = defaultdict(lambda: {
        m: {"P":0,"A":0,"WOP":0,"HP":0,"OT":0} for m in range(1,13)
    })
    for row in att_rows:
        ec = row["emp_code"]
        m  = row["month"]
        st = row["status"] or ""
        ot = row["ot_minutes"] or 0
        if st in ("Present","WOP","P"): 
            if st == "WOP": emp_monthly[ec][m]["WOP"] += 1
            else:           emp_monthly[ec][m]["P"]   += 1
        elif st in ("Absent","A"):      emp_monthly[ec][m]["A"]   += 1
        elif st in ("HP","Half Day"):   emp_monthly[ec][m]["HP"]  += 1
        elif st == "WOP":               emp_monthly[ec][m]["WOP"] += 1
        emp_monthly[ec][m]["OT"] += ot

    # Build final data
    emp_info = {e["emp_code"]: dict(e) for e in emps}
    emp_data = []
    for ec in emp_codes:
        info = emp_info[ec]
        monthly = []
        year_totals = {"P":0,"A":0,"WOP":0,"HP":0,"OT":0}
        for m in range(1,13):
            md = emp_monthly[ec][m]
            ot_hrs = round(md["OT"]/60, 1)
            monthly.append({"P":md["P"],"A":md["A"],"WOP":md["WOP"],"HP":md["HP"],"OT":ot_hrs})
            year_totals["P"]   += md["P"]
            year_totals["A"]   += md["A"]
            year_totals["WOP"] += md["WOP"]
            year_totals["HP"]  += md["HP"]
            year_totals["OT"]  += md["OT"]
        year_totals["OT"] = round(year_totals["OT"]/60, 1)
        emp_data.append({
            "emp_code":   ec,
            "emp_name":   info["emp_name"],
            "department": info["department"] or "",
            "category":   info["category"] or "",
            "scheme":     info["scheme_name"] or "",
            "monthly":    monthly,
            "totals":     year_totals
        })

    depts   = conn.execute("SELECT DISTINCT department FROM employees WHERE status='Active' AND department IS NOT NULL ORDER BY department").fetchall()
    schemes = conn.execute("SELECT scheme_name FROM employee_schemes WHERE is_active=1 ORDER BY scheme_name").fetchall()
    conn.close()

    return render_template("yearly_attendance.html",
        year=year, emp_data=emp_data, months=MONTHS,
        departments=[d["department"] for d in depts],
        categories=["Staff","Associate"],
        schemes=[s["scheme_name"] for s in schemes],
        selected_dept=dept, selected_cat=cat, selected_scheme=scheme,
        years=list(range(2020, date.today().year+2)))


@app.route("/export/yearly-attendance")
@amgr
def export_yearly_attendance():
    """Export yearly attendance to Excel"""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    from collections import defaultdict

    year   = int(request.args.get("year", date.today().year))
    dept   = request.args.get("dept","")
    cat    = request.args.get("cat","")
    scheme = request.args.get("scheme","")
    conn   = get_db()

    extra = " AND e.status='Active'"
    params = []
    if dept:   extra += " AND e.department=?"; params.append(dept)
    if cat:    extra += " AND e.category=?";   params.append(cat)
    if scheme:
        _sc = conn.execute("SELECT id FROM employee_schemes WHERE scheme_name=? AND is_active=1",(scheme,)).fetchone()
        if _sc: extra += " AND e.scheme_id=?"; params.append(_sc["id"])

    emps = conn.execute(f"""
        SELECT e.emp_code, e.emp_name, e.department, e.category,
               COALESCE(es.scheme_name,'') as scheme_name
        FROM employees e LEFT JOIN employee_schemes es ON e.scheme_id=es.id
        WHERE 1=1{extra} ORDER BY e.department, e.emp_name
    """, params).fetchall()

    emp_codes = [e["emp_code"] for e in emps]
    ph = ",".join("?"*len(emp_codes)) if emp_codes else "''"
    att_rows = conn.execute(f"""
        SELECT emp_code, CAST(strftime('%m',att_date) AS INTEGER) as month,
               status, ot_minutes
        FROM attendance WHERE strftime('%Y',att_date)=? AND emp_code IN ({ph})
    """, [str(year)] + emp_codes).fetchall() if emp_codes else []
    conn.close()

    emp_monthly = defaultdict(lambda: {m:{"P":0,"A":0,"WOP":0,"HP":0,"OT":0} for m in range(1,13)})
    for row in att_rows:
        ec=row["emp_code"]; m=row["month"]; st=row["status"] or ""; ot=row["ot_minutes"] or 0
        if st=="WOP":       emp_monthly[ec][m]["WOP"]+=1
        elif st in("Present","P"): emp_monthly[ec][m]["P"]+=1
        elif st in("Absent","A"):  emp_monthly[ec][m]["A"]+=1
        elif st in("HP","Half Day"): emp_monthly[ec][m]["HP"]+=1
        emp_monthly[ec][m]["OT"]+=ot

    wb = openpyxl.Workbook(); ws = wb.active
    ws.title = f"Yearly Attendance {year}"
    MNS = ["Jan","Feb","Mar","Apr","May","Jun","Jul","Aug","Sep","Oct","Nov","Dec"]

    # Header row 1 - merged groups
    ws.merge_cells("A1:E1"); ws["A1"]="Employee Details"
    ws["A1"].font=Font(bold=True,color="FFFFFF"); ws["A1"].fill=PatternFill("solid",fgColor="0052CC")
    ws["A1"].alignment=Alignment(horizontal="center")
    col=6
    for mn in MNS:
        ws.merge_cells(start_row=1,start_column=col,end_row=1,end_column=col+4)
        c=ws.cell(1,col,mn)
        c.font=Font(bold=True,color="FFFFFF"); c.fill=PatternFill("solid",fgColor="1E3A5F")
        c.alignment=Alignment(horizontal="center")
        col+=5
    ws.merge_cells(start_row=1,start_column=col,end_row=1,end_column=col+4)
    c=ws.cell(1,col,"YEARLY TOTAL")
    c.font=Font(bold=True,color="FFFFFF"); c.fill=PatternFill("solid",fgColor="7C3AED")
    c.alignment=Alignment(horizontal="center")

    # Header row 2
    sub=["Code","Name","Dept","Category","Scheme"]
    for s in sub: ws.cell(2,len(sub)-len(sub)+sub.index(s)+1,s).font=Font(bold=True)
    col=6
    for _ in MNS+["TOTAL"]:
        for s in ["P","A","WOP","HP","OT Hrs"]:
            c=ws.cell(2,col,s); c.font=Font(bold=True); col+=1

    # Data rows
    row_num=3
    for e in emps:
        ec=e["emp_code"]
        row=[ec,e["emp_name"],e["department"],e["category"],e["scheme_name"]]
        ytot={"P":0,"A":0,"WOP":0,"HP":0,"OT":0}
        for m in range(1,13):
            md=emp_monthly[ec][m]
            row+=[md["P"],md["A"],md["WOP"],md["HP"],round(md["OT"]/60,1)]
            ytot["P"]+=md["P"]; ytot["A"]+=md["A"]; ytot["WOP"]+=md["WOP"]
            ytot["HP"]+=md["HP"]; ytot["OT"]+=md["OT"]
        row+=[ytot["P"],ytot["A"],ytot["WOP"],ytot["HP"],round(ytot["OT"]/60,1)]
        ws.append(row); row_num+=1

    # Column widths
    ws.column_dimensions["A"].width=10; ws.column_dimensions["B"].width=24
    ws.column_dimensions["C"].width=15; ws.column_dimensions["D"].width=12
    ws.column_dimensions["E"].width=14
    for i in range(6, 6+13*5):
        ws.column_dimensions[get_column_letter(i)].width=7

    return xlresp(wb, f"Yearly_Attendance_{year}.xlsx")


# ─── END YEARLY ATTENDANCE ─────────────────────────────────


@app.route("/reports/absent-report")
@amgr
def absent_report():
    """Absent Report with date range, category, department filters.
    Also includes Miss Punch records (separate filter option).
    """
    from_date   = request.args.get("from_date", "")
    to_date     = request.args.get("to_date", "")
    dept        = request.args.get("dept", "")
    cat         = request.args.get("cat", "")
    emp_search  = request.args.get("emp", "")
    # show_mp: include Miss Punch records alongside Absent (default: yes)
    show_mp     = request.args.get("show_mp", "1") != "0"

    conn = get_db()

    status_filter = "a.status IN ('Absent','Miss Punch')" if show_mp else "a.status='Absent'"

    # Build query for employees who have an Absent/Miss Punch record
    sql = f"""SELECT a.emp_code, a.att_date, a.status as att_status,
        e.emp_name, e.department, e.category
        FROM attendance a
        JOIN employees e ON a.emp_code=e.emp_code
        WHERE {status_filter}"""
    params = []

    if from_date:
        sql += " AND a.att_date >= ?"; params.append(from_date)
    if to_date:
        sql += " AND a.att_date <= ?"; params.append(to_date)
    if dept:
        sql += " AND e.department=?"; params.append(dept)
    if cat:
        sql += " AND e.category=?"; params.append(cat)
    if emp_search:
        sql += " AND (e.emp_code LIKE ? OR e.emp_name LIKE ?)"; params += [f"%{emp_search}%", f"%{emp_search}%"]

    sql += " ORDER BY a.att_date, e.emp_name"
    rows = conn.execute(sql, params).fetchall()

    # For single-day filter: also include employees with NO attendance record (truly absent)
    from collections import defaultdict
    from datetime import datetime as _dtnow_ar
    extra_rows = []
    if from_date and to_date and from_date == to_date:
        chk_date = from_date
        _now_min_ar = _dtnow_ar.now().hour * 60 + _dtnow_ar.now().minute
        # All active employees not on WO today
        _today_dow_ar = date.today().weekday()
        _wo_names_ar  = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
        _today_wo_ar  = _wo_names_ar[_today_dow_ar]
        emp_sql = "SELECT emp_code,emp_name,department,category FROM employees WHERE status='Active'"
        emp_par = []
        if dept:   emp_sql += " AND department=?"; emp_par.append(dept)
        if cat:    emp_sql += " AND category=?";   emp_par.append(cat)
        if emp_search: emp_sql += " AND (emp_code LIKE ? OR emp_name LIKE ?)"; emp_par += [f"%{emp_search}%",f"%{emp_search}%"]
        all_emps = conn.execute(emp_sql, emp_par).fetchall()

        # Employees who have any attendance record today
        have_att = {r["emp_code"] for r in conn.execute(
            "SELECT DISTINCT emp_code FROM attendance WHERE att_date=?", (chk_date,)).fetchall()}

        # Employees whose shift hasn't started yet (yet-to-arrive) — exclude from absent
        shifts_today = conn.execute("""
            SELECT srd.emp_code, s.start_time FROM shift_roster_dates srd
            JOIN shifts s ON srd.shift_id=s.id
            JOIN employees e ON srd.emp_code=e.emp_code
            WHERE srd.shift_date=? AND e.status='Active'
        """, (chk_date,)).fetchall()
        yet_arrive_ar = set()
        for st in shifts_today:
            try:
                sh_h, sh_m = map(int, (st["start_time"] or "09:00").split(":")[:2])
                if _now_min_ar < sh_h*60 + sh_m + 30:  # 30 min grace
                    yet_arrive_ar.add(st["emp_code"])
            except: pass

        for emp in all_emps:
            ec = emp["emp_code"]
            if ec in have_att: continue           # has record (present/absent/WO etc.)
            if ec in yet_arrive_ar: continue       # shift not started yet
            # Check if today is their weekly off
            wo = conn.execute("SELECT COALESCE(weekly_off,'Sunday') as wo FROM employees WHERE emp_code=?", (ec,)).fetchone()
            if wo and wo["wo"] == _today_wo_ar: continue  # weekly off
            extra_rows.append({
                "emp_code": ec, "att_date": chk_date,
                "att_status": "No Record (Absent)", "emp_name": emp["emp_name"],
                "department": emp["department"], "category": emp["category"]
            })

    depts = [d["department"] for d in conn.execute(
        "SELECT DISTINCT department FROM employees WHERE status='Active' AND department IS NOT NULL ORDER BY department"
    ).fetchall()]
    conn.close()

    all_rows = [dict(r) for r in rows] + extra_rows
    all_rows.sort(key=lambda x: (x["att_date"], x["emp_name"]))

    # Summary by employee
    emp_summary = defaultdict(lambda: {"emp_name":"","department":"","category":"","count":0,"dates":[]})
    for r in all_rows:
        ec = r["emp_code"]
        emp_summary[ec]["emp_name"]   = r["emp_name"]
        emp_summary[ec]["department"] = r["department"]
        emp_summary[ec]["category"]   = r["category"]
        emp_summary[ec]["count"]      += 1
        emp_summary[ec]["dates"].append(r["att_date"])

    return render_template("absent_report.html",
        rows=all_rows,
        emp_summary=dict(emp_summary),
        from_date=from_date, to_date=to_date,
        dept=dept, cat=cat, emp_search=emp_search,
        departments=depts,
        total_absent=len(all_rows))


@app.route("/reports/absent-export")
@amgr
def absent_report_export():
    """Export absent report to Excel"""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    from_date  = request.args.get("from_date","")
    to_date    = request.args.get("to_date","")
    dept       = request.args.get("dept","")
    cat        = request.args.get("cat","")
    emp_search = request.args.get("emp","")

    conn = get_db()
    sql = """SELECT a.emp_code, a.att_date, e.emp_name, e.department, e.category
        FROM attendance a JOIN employees e ON a.emp_code=e.emp_code
        WHERE a.status IN ('Absent','Miss Punch')"""
    params=[]
    if from_date: sql+=" AND a.att_date>=?"; params.append(from_date)
    if to_date:   sql+=" AND a.att_date<=?"; params.append(to_date)
    if dept:      sql+=" AND e.department=?"; params.append(dept)
    if cat:       sql+=" AND e.category=?"; params.append(cat)
    if emp_search: sql+=" AND (e.emp_code LIKE ? OR e.emp_name LIKE ?)"; params+=[f"%{emp_search}%",f"%{emp_search}%"]
    sql+=" ORDER BY a.att_date, e.emp_name"
    rows=conn.execute(sql,params).fetchall()

    # Single-day: include employees with NO attendance record (truly absent)
    extra_exp = []
    if from_date and to_date and from_date == to_date:
        from datetime import datetime as _dtnow_exp
        _now_min_exp = _dtnow_exp.now().hour * 60 + _dtnow_exp.now().minute
        _today_dow_exp = date.today().weekday()
        _wo_names_exp = ["Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"]
        _today_wo_exp = _wo_names_exp[_today_dow_exp]
        emp_sql2 = "SELECT emp_code,emp_name,department,category FROM employees WHERE status='Active'"
        emp_par2 = []
        if dept:   emp_sql2+=" AND department=?"; emp_par2.append(dept)
        if cat:    emp_sql2+=" AND category=?"; emp_par2.append(cat)
        all_emps2 = conn.execute(emp_sql2, emp_par2).fetchall()
        have_att2 = {r["emp_code"] for r in conn.execute(
            "SELECT DISTINCT emp_code FROM attendance WHERE att_date=?", (from_date,)).fetchall()}
        shifts_exp = conn.execute("""SELECT srd.emp_code, s.start_time FROM shift_roster_dates srd
            JOIN shifts s ON srd.shift_id=s.id JOIN employees e ON srd.emp_code=e.emp_code
            WHERE srd.shift_date=? AND e.status='Active'""", (from_date,)).fetchall()
        yet_ar_exp = set()
        for st in shifts_exp:
            try:
                sh_h, sh_m = map(int, (st["start_time"] or "09:00").split(":")[:2])
                if _now_min_exp < sh_h*60 + sh_m + 30: yet_ar_exp.add(st["emp_code"])
            except: pass
        for emp2 in all_emps2:
            ec2 = emp2["emp_code"]
            if ec2 in have_att2 or ec2 in yet_ar_exp: continue
            wo2 = conn.execute("SELECT COALESCE(weekly_off,'Sunday') as wo FROM employees WHERE emp_code=?", (ec2,)).fetchone()
            if wo2 and wo2["wo"] == _today_wo_exp: continue
            if emp_search and emp_search.lower() not in ec2.lower() and emp_search.lower() not in (emp2["emp_name"] or "").lower(): continue
            extra_exp.append({"emp_code":ec2,"att_date":from_date,
                "emp_name":emp2["emp_name"],"department":emp2["department"],"category":emp2["category"]})
    conn.close()

    all_exp = [dict(r) for r in rows] + extra_exp
    all_exp.sort(key=lambda x: (x["att_date"], x["emp_name"]))

    wb=openpyxl.Workbook(); ws=wb.active
    ws.title=f"Absent Report"
    ws.merge_cells("A1:E1")
    ws["A1"]=f"VIJAYSHRI PACKAGING LTD. — Absent Report | {from_date} to {to_date}"
    ws["A1"].font=Font(bold=True,size=11,color="FFFFFF")
    ws["A1"].fill=PatternFill("solid",fgColor="0052CC")
    ws["A1"].alignment=Alignment(horizontal="center")

    hdrs=["Date","Emp Code","Name","Department","Category"]
    for ci,h in enumerate(hdrs,1):
        cell=ws.cell(2,ci,h)
        cell.font=Font(bold=True,color="FFFFFF")
        cell.fill=PatternFill("solid",fgColor="1E3A5F")

    for r in all_exp:
        ws.append([r["att_date"],r["emp_code"],r["emp_name"],r["department"],r["category"]])

    for i,w in enumerate([14,12,24,18,12],1):
        ws.column_dimensions[get_column_letter(i)].width=w

    return xlresp(wb, f"Absent_Report_{from_date}_{to_date}.xlsx")

@app.route("/reports")
@amgr
def reports():
    conn=get_db()
    m   = int(request.args.get("month",date.today().month))
    y   = int(request.args.get("year", date.today().year))
    cat = request.args.get("cat","")
    scheme_filter = request.args.get("scheme","")
    cat_sql = ""
    if cat == "Staff":    cat_sql = " AND e.category='Staff'"
    elif cat == "NonStaff": cat_sql = " AND e.category!='Staff'"
    scheme_sql = ""; scheme_params = []
    if scheme_filter:
        sc = conn.execute("SELECT id FROM employee_schemes WHERE scheme_name=? AND is_active=1",(scheme_filter,)).fetchone()
        if sc: scheme_sql = " AND e.scheme_id=?"; scheme_params = [sc["id"]]
    r_dept_sql, r_dept_params = get_user_dept_filter("e")
    base_params = [m,y]+r_dept_params+scheme_params
    dp = conn.execute(f"""SELECT e.department,SUM(s.net_salary) as total,COUNT(*) as ec,
        SUM(s.gross) as gt,SUM(s.pf) as pt,SUM(s.esi) as et
        FROM salary_records s JOIN employees e ON s.emp_code=e.emp_code
        WHERE s.month=? AND s.year=?{cat_sql}{r_dept_sql}{scheme_sql} GROUP BY e.department""",
        base_params).fetchall()
    mt = conn.execute(f"""SELECT month,year,SUM(net_salary) as total,COUNT(*) as ec
        FROM salary_records s JOIN employees e ON s.emp_code=e.emp_code
        WHERE 1=1{cat_sql}{scheme_sql} GROUP BY year,month ORDER BY year DESC,month DESC LIMIT 12""",
        scheme_params).fetchall()
    cd = conn.execute("SELECT category,COUNT(*) as count FROM employees WHERE status='Active' GROUP BY category").fetchall()
    te = conn.execute(f"""SELECT s.emp_code,e.emp_name,e.department,e.category,s.net_salary,s.present_days,s.working_days
        FROM salary_records s JOIN employees e ON s.emp_code=e.emp_code
        WHERE s.month=? AND s.year=?{cat_sql}{scheme_sql} ORDER BY s.net_salary DESC LIMIT 5""",
        [m,y]+scheme_params).fetchall()
    conn.close()
    tp=sum(r["total"] for r in dp) if dp else 0
    conn2 = get_db()
    emps_list = conn2.execute("SELECT emp_code,emp_name,department FROM employees WHERE status='Active' ORDER BY emp_name").fetchall()
    depts_list = conn2.execute("SELECT DISTINCT department FROM employees WHERE status='Active' AND department IS NOT NULL ORDER BY department").fetchall()
    schemes_list = conn2.execute("SELECT scheme_name FROM employee_schemes WHERE is_active=1 ORDER BY scheme_name").fetchall()
    conn2.close()
    # att_rows NOT loaded on page load — fetched via AJAX when user clicks Load
    return render_template("reports.html",dept_payout=dp,monthly_trend=mt,category_data=cd,
        top_earners=te,month_name=MONTHS[m-1],year=y,months=MONTHS,
        total_pay=tp,total_pf=sum(r["pt"] for r in dp) if dp else 0,
        total_esi=sum(r["et"] for r in dp) if dp else 0,
        total_gross=sum(r["gt"] for r in dp) if dp else 0,
        cur_month=m,cur_year=y,cat_filter=cat,scheme_filter=scheme_filter,
        employees=emps_list, departments=[d["department"] for d in depts_list],
        schemes=[s["scheme_name"] for s in schemes_list],
        att_rows=[],
        today_month=date.today().month,
        today_year=date.today().year, enumerate=enumerate)


@app.route("/reports/att-data")
@amgr
def reports_att_data():
    """AJAX: fetch attendance rows for any month/year or specific date"""
    from_date = request.args.get("from_date","")
    to_date   = request.args.get("to_date","")
    emp_code  = request.args.get("emp_code","")
    dept      = request.args.get("dept","")
    m         = request.args.get("month","")
    y         = request.args.get("year","")

    conn = get_db()
    params = []
    where  = "1=1"

    if from_date and to_date:
        where += " AND a.att_date BETWEEN ? AND ?"
        params += [from_date, to_date]
    elif from_date:
        where += " AND a.att_date=?"
        params += [from_date]
    elif m and y:
        where += " AND strftime('%m',a.att_date)=? AND strftime('%Y',a.att_date)=?"
        params += [f"{int(m):02d}", str(y)]

    scheme    = request.args.get("scheme","")

    if emp_code:
        where += " AND a.emp_code=?"
        params.append(emp_code)
    if dept:
        where += " AND e.department=?"
        params.append(dept)
    if scheme:
        sc = conn.execute("SELECT id FROM employee_schemes WHERE scheme_name=? AND is_active=1",(scheme,)).fetchone()
        if sc:
            where += " AND e.scheme_id=?"
            params.append(sc["id"])

    rows = conn.execute(f"""SELECT a.*, e.emp_name, e.department, e.category,
        COALESCE(es.scheme_name,'') as scheme_name
        FROM attendance a JOIN employees e ON a.emp_code=e.emp_code
        LEFT JOIN employee_schemes es ON e.scheme_id=es.id
        WHERE {where}
        ORDER BY e.emp_name, a.att_date
        LIMIT 100000""", params).fetchall()
    conn.close()

    return jsonify([dict(r) for r in rows])

@app.route("/reports/data")
@amgr
def reports_data():
    m   = int(request.args.get("month",date.today().month))
    y   = int(request.args.get("year", date.today().year))
    cat = request.args.get("cat","")
    cat_sql = ""
    if cat == "Staff":    cat_sql = " AND e.category='Staff'"
    elif cat == "NonStaff": cat_sql = " AND e.category!='Staff'"
    conn=get_db()
    dp=conn.execute(f"""SELECT e.department,SUM(s.net_salary) as total,COUNT(*) as ec,
        SUM(s.gross) as gt,SUM(s.pf) as pt,SUM(s.esi) as et
        FROM salary_records s JOIN employees e ON s.emp_code=e.emp_code
        WHERE s.month=? AND s.year=?{cat_sql} GROUP BY e.department""",(m,y)).fetchall()
    te=conn.execute(f"""SELECT s.emp_code,e.emp_name,e.department,e.category,s.net_salary,s.present_days,s.working_days
        FROM salary_records s JOIN employees e ON s.emp_code=e.emp_code
        WHERE s.month=? AND s.year=?{cat_sql} ORDER BY s.net_salary DESC LIMIT 5""",(m,y)).fetchall()
    conn.close(); tp=sum(r["total"] for r in dp) if dp else 0
    return jsonify({"month_name":MONTHS[m-1],"year":y,"total_pay":tp,
        "total_pf":sum(r["pt"] for r in dp) if dp else 0,
        "total_esi":sum(r["et"] for r in dp) if dp else 0,
        "total_gross":sum(r["gt"] for r in dp) if dp else 0,
        "dept_payout":[dict(r) for r in dp],"top_earners":[dict(r) for r in te]})

@app.route("/calendar")
@lreq
def cal_page():
    m=int(request.args.get("month",date.today().month))
    y=int(request.args.get("year", date.today().year))
    conn=get_db()
    hols=conn.execute("SELECT * FROM holidays WHERE strftime('%Y-%m',holiday_date)=?",
                     (f"{y}-{m:02d}",)).fetchall()
    conn.close()
    hol_dict={h["holiday_date"]:dict(h) for h in hols}
    return render_template("calendar.html",cal=calendar.monthcalendar(y,m),
        month=m,year=y,months=MONTHS,month_name=MONTHS[m-1],today=date.today(),
        holidays=hol_dict)


# ─── USER PERMISSIONS ──────────────────────────────────────

def get_user_permissions(user_id, conn):
    try:
        rows = conn.execute("SELECT permission FROM user_permissions WHERE user_id=?", (user_id,)).fetchall()
        return {r["permission"] for r in rows}
    except:
        return set()

def has_permission(perm):
    """Check if current session user has a permission"""
    role = session.get("role","")
    if role == "admin": return True  # admin has all
    # Get from session cache
    perms = session.get("permissions", [])
    return perm in perms

@app.route("/users/permissions/<int:uid>", methods=["GET","POST"])
@amgr
def user_permissions_route(uid):
    conn = get_db()
    if request.method == "POST":
        d = request.json
        new_perms = d.get("permissions", [])
        new_depts = d.get("dept_access", [])  # department access list
        try:
            conn.execute("DELETE FROM user_permissions WHERE user_id=?", (uid,))
            for p in new_perms:
                conn.execute("INSERT OR IGNORE INTO user_permissions (user_id, permission) VALUES (?,?)", (uid, p))
            # Save dept access
            conn.execute("DELETE FROM user_dept_access WHERE user_id=?", (uid,))
            for dept in new_depts:
                if dept.strip():
                    conn.execute("INSERT OR IGNORE INTO user_dept_access (user_id, department) VALUES (?,?)", (uid, dept.strip()))
            conn.commit()
        except Exception as e:
            conn.close()
            return jsonify({"success":False,"error":str(e)})
        conn.close()
        return jsonify({"success": True, "message": f"{len(new_perms)} permissions saved"})
    # GET
    try:
        user = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not user: conn.close(); return jsonify({"error": "User not found"})
        perms = get_user_permissions(uid, conn)
        dept_rows = conn.execute("SELECT department FROM user_dept_access WHERE user_id=?", (uid,)).fetchall()
        dept_access = [r["department"] for r in dept_rows]
        conn.close()
        return jsonify({"success": True, "permissions": list(perms), "dept_access": dept_access, "username": user["username"]})
    except Exception as e:
        conn.close()
        return jsonify({"success": False, "error": str(e), "permissions": []})

@app.route("/users/add", methods=["POST"])
@amgr
def add_user():
    d = request.json; conn = get_db()
    try:
        # Default permissions for new employee users
        default_perms = ["my_payslip", "my_leaves"]
        conn.execute("""INSERT INTO users (username, password, role, emp_id, name, is_active, permissions)
            VALUES (?,?,?,?,?,1,?)""",
            (d["username"], hp(d["password"]), d.get("role","employee"),
             d.get("emp_id",""), d.get("name",""), ",".join(default_perms)))
        conn.commit()
        uid = conn.execute("SELECT id FROM users WHERE username=?", (d["username"],)).fetchone()["id"]
        for p in default_perms:
            conn.execute("INSERT OR IGNORE INTO user_permissions (user_id, permission) VALUES (?,?)", (uid, p))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        conn.close()
        return jsonify({"success": False, "error": str(e)})

@app.route("/users/toggle/<int:uid>", methods=["POST"])
@amgr
def toggle_user(uid):
    conn = get_db()
    conn.execute("UPDATE users SET is_active = 1 - is_active WHERE id=?", (uid,))
    conn.commit(); conn.close()
    return jsonify({"success": True})



@app.route("/admin/settings", methods=["GET","POST"])
@amgr
def admin_settings():
    conn = get_db()
    if request.method == "POST":
        f = request.files.get("logo")
        if f and f.filename:
            data = f.read()
            conn.execute("""UPDATE company_settings SET logo=?, logo_filename=?, updated_on=datetime('now')
                WHERE id=1""", (data, f.filename))
            conn.commit()
            flash_msg = "Logo updated!"
        else:
            flash_msg = "No file selected"
        settings = conn.execute("SELECT * FROM company_settings WHERE id=1").fetchone()
        conn.close()
        return render_template("admin_settings.html", settings=dict(settings) if settings else {}, msg=flash_msg)
    settings = conn.execute("SELECT * FROM company_settings WHERE id=1").fetchone()
    conn.close()
    return render_template("admin_settings.html", settings=dict(settings) if settings else {})

@app.route("/admin/logo")
def company_logo():
    conn = get_db()
    row = conn.execute("SELECT logo FROM company_settings WHERE id=1").fetchone()
    conn.close()
    if row and row["logo"]:
        from flask import Response
        return Response(row["logo"], mimetype="image/png",
            headers={"Cache-Control":"public,max-age=3600"})
    # Return transparent 1x1 PNG instead of 404 to avoid CMD errors
    import base64
    _empty_png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
    from flask import Response
    return Response(_empty_png, mimetype="image/png",
        headers={"Cache-Control":"public,max-age=60"})

@app.route("/admin/logo/delete", methods=["POST"])
@amgr
def delete_logo():
    conn = get_db()
    conn.execute("UPDATE company_settings SET logo=NULL, logo_filename=NULL WHERE id=1")
    conn.commit(); conn.close()
    return jsonify({"success": True})

@app.route("/users/activity-log")
@amgr
def user_activity_log():
    uid      = request.args.get("uid","")
    uname    = request.args.get("username","")
    from_d   = request.args.get("from","")
    to_d     = request.args.get("to","")
    action_f = request.args.get("action","")
    conn = get_db()
    sql = "SELECT * FROM activity_log WHERE 1=1"
    params = []
    if uid:     sql+=" AND user_id=?"; params.append(uid)
    if uname:   sql+=" AND username LIKE ?"; params.append(f"%{uname}%")
    if from_d:  sql+=" AND logged_at>=?"; params.append(from_d)
    if to_d:    sql+=" AND logged_at<=?"; params.append(to_d+" 23:59:59")
    if action_f:sql+=" AND action=?"; params.append(action_f)
    sql += " ORDER BY logged_at DESC LIMIT 500"
    logs = conn.execute(sql, params).fetchall()
    users = conn.execute("SELECT id,username,name FROM users ORDER BY username").fetchall()
    conn.close()
    return render_template("activity_log.html", logs=logs, users=users,
        uid=uid, uname=uname, from_d=from_d, to_d=to_d, action_f=action_f)

@app.route("/users")
@amgr
def manage_users():
    conn=get_db()
    users=conn.execute("SELECT * FROM users ORDER BY role,username").fetchall()
    emps=conn.execute("SELECT emp_code,emp_name FROM employees WHERE status='Active' ORDER BY emp_name").fetchall()
    depts = conn.execute("SELECT DISTINCT department FROM employees WHERE status='Active' AND department IS NOT NULL ORDER BY department").fetchall()
    conn.close()
    return render_template("users.html", users=users, employees=emps,
                          all_permissions=PERMISSIONS, permission_tree=PERMISSION_TREE,
                          all_departments=[d["department"] for d in depts])

@app.route("/users/reset-password",methods=["POST"])
@amgr
def reset_pw():
    d=request.json; conn=get_db()
    conn.execute("UPDATE users SET password=? WHERE username=?",(hp(d["password"]),d["username"]))
    conn.commit(); conn.close(); return jsonify({"success":True})

@app.route("/celebrations")
@lreq
def celebrations():
    cel=get_celebrations()
    return jsonify(cel)

# ─── EXPORTS ──────────────────────────────────────────
def make_wb(title,headers,rows,widths,month_name,year,hcol="0052CC"):
    import openpyxl
    from openpyxl.styles import Font,PatternFill,Alignment,Border,Side
    wb=openpyxl.Workbook(); ws=wb.active; ws.title=f"{month_name} {year}"
    thin=Border(**{s:Side(style="thin",color="D0DCF0") for s in ["left","right","top","bottom"]})
    cl=openpyxl.utils.get_column_letter
    ws.merge_cells(f"A1:{cl(len(headers))}1")
    ws["A1"]=f"{COMPANY} — {title}: {month_name} {year}"
    ws["A1"].font=Font(bold=True,size=13,color="FFFFFF")
    ws["A1"].fill=PatternFill("solid",fgColor=hcol)
    ws["A1"].alignment=Alignment(horizontal="center"); ws.row_dimensions[1].height=26
    for i,h in enumerate(headers,1):
        c=ws.cell(row=2,column=i,value=h)
        c.font=Font(bold=True,size=10,color="FFFFFF"); c.fill=PatternFill("solid",fgColor="1E2A40")
        c.alignment=Alignment(horizontal="center",wrap_text=True); c.border=thin
    ws.row_dimensions[2].height=24
    for ri,row in enumerate(rows,3):
        bg="0E1422" if ri%2==0 else "121929"
        for ci,val in enumerate(row,1):
            c=ws.cell(row=ri,column=ci,value=val)
            c.fill=PatternFill("solid",fgColor=bg); c.border=thin
            c.font=Font(size=10,color="E2E8F0"); c.alignment=Alignment(horizontal="center")
            if ci==2: c.alignment=Alignment(horizontal="left")
    for i,w in enumerate(widths,1): ws.column_dimensions[cl(i)].width=w
    return wb

def xlresp(wb,fname):
    out=io.BytesIO(); wb.save(out); out.seek(0)
    return send_file(out,mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    as_attachment=True,download_name=fname)

@app.route("/export/salary/<int:m>/<int:y>")
@amgr
def exp_salary(m,y):
    emp_code=request.args.get("emp_code","").strip()
    dept_f=request.args.get("dept","").strip()
    cat=request.args.get("cat","")
    scheme_f=request.args.get("scheme","").strip()
    cat_sql=""
    if cat=="Staff": cat_sql=" AND e.category='Staff'"
    elif cat=="NonStaff": cat_sql=" AND e.category!='Staff'"
    dept_sql=""; scheme_sql=""
    exp_params=[m,y]
    if dept_f: dept_sql=" AND e.department=?"; exp_params.append(dept_f)
    if emp_code: dept_sql+=" AND s.emp_code=?"; exp_params.append(emp_code)
    if scheme_f:
        conn_s=get_db(); sc=conn_s.execute("SELECT id FROM employee_schemes WHERE scheme_name=? AND is_active=1",(scheme_f,)).fetchone(); conn_s.close()
        if sc: scheme_sql=" AND e.scheme_id=?"; exp_params.append(sc["id"])
    conn=get_db()
    rows=conn.execute(f"""SELECT s.emp_code,e.emp_name,e.category,e.department,e.designation,
        s.present_days,
        COALESCE(s.payable_days, s.present_days) as payable_days,
        COALESCE(s.paid_leave_days,0) as paid_leave_days,
        COALESCE(s.absent_days,0) as absent_days,
        COALESCE(s.half_days,0) as half_days,
        COALESCE(s.late_marks,0) as late_marks,
        s.working_days,
        e.basic as actual_basic,
        e.hra as actual_hra,
        COALESCE(e.basic,0)+COALESCE(e.hra,0)+COALESCE(e.special_allowance,0) as actual_gross,
        s.basic_earned,s.hra_earned,s.special_earned,
        s.ot_hours,s.ot_amount,s.gross,s.pf,s.esi,
        COALESCE(s.pt,0) as pt,
        s.tds,
        COALESCE(s.loan_deduction,0) as loan_deduction,
        COALESCE(s.canteen_deduction,0) as canteen_deduction,
        COALESCE(s.fine_deduction,0) as fine_deduction,
        s.total_deductions,s.net_salary,
        e.bank_account,e.bank_name,e.ifsc,e.pan,e.uan_number,e.pf_number,e.esic_number
        FROM salary_records s JOIN employees e ON s.emp_code=e.emp_code
        WHERE s.month=? AND s.year=?{cat_sql}{dept_sql}{scheme_sql} ORDER BY e.category,e.emp_name""",exp_params).fetchall()
    conn.close()
    data_rows = []
    for r in rows:
        def rnd(v): return round(float(v or 0))
        data_rows.append([
            r["emp_code"], r["emp_name"], r["category"], r["department"] or "", r["designation"] or "",
            r["working_days"],
            r["payable_days"],
            r["paid_leave_days"],
            r["absent_days"],
            r["half_days"],
            r["late_marks"],
            rnd(r["actual_basic"]), rnd(r["actual_hra"]), rnd(r["actual_gross"]),
            rnd(r["basic_earned"]), rnd(r["hra_earned"]), rnd(r["special_earned"]),
            r["ot_hours"], rnd(r["ot_amount"]), rnd(r["gross"]),
            rnd(r["pf"]), rnd(r["esi"]), rnd(r["pt"]), rnd(r["tds"]),
            rnd(r["loan_deduction"]), rnd(r["canteen_deduction"]), rnd(r["fine_deduction"]),
            rnd(r["total_deductions"]), rnd(r["net_salary"]),
            r["bank_account"] or "", r["bank_name"] or "", r["ifsc"] or "", r["pan"] or "",
            r["uan_number"] or "", r["pf_number"] or "", r["esic_number"] or ""
        ])
    hdrs=["Emp Code","Name","Category","Department","Designation",
          "Working Days","Payable Days","Paid Leave","Absent Days","Half Days","Late Marks",
          "Actual Basic","Actual HRA","Actual Gross",
          "Earned Basic","Earned HRA","Earned Special","OT Hrs","OT Amt","Gross",
          "PF","ESI","PT","TDS","Loan/Advance","Canteen/Uniform","Fine/Penalty",
          "Total Ded","Net Salary",
          "Bank Account","Bank","IFSC","PAN","UAN Number","PF Number","ESIC Number"]
    widths=[10,22,12,16,18,
            10,12,10,10,10,10,
            13,13,14,
            13,13,13,9,12,14,
            12,12,10,12,14,14,14,
            14,14,
            18,16,14,14,14,16,16]
    wb=make_wb("Salary Register",hdrs,data_rows,widths,MONTHS[m-1],y)
    return xlresp(wb,f"Salary_{MONTHS[m-1]}_{y}.xlsx")

@app.route("/export/attendance/<int:m>/<int:y>")
@amgr
def exp_att(m,y):
    cat      = request.args.get("cat","")
    emp_code = request.args.get("emp_code","").strip()
    dept     = request.args.get("dept","").strip()
    conn = get_db()

    sql = "SELECT * FROM employees WHERE status='Active'"
    params = []
    if cat == "Staff":     sql += " AND category='Staff'"
    elif cat == "NonStaff": sql += " AND category!='Staff'"
    if emp_code: sql += " AND emp_code=?";   params.append(emp_code)
    if dept:     sql += " AND department=?"; params.append(dept)
    sql += " ORDER BY category,emp_name"
    emps = conn.execute(sql, params).fetchall()

    # ── Fetch working days ONCE (not per employee) ──────────
    try:
        wd_row = conn.execute(
            "SELECT staff_days, nonstaff_days FROM monthly_working_days WHERE year=? AND month=?",
            (y, m)).fetchone()
    except:
        wd_row = None

    if wd_row:
        wd_staff  = wd_row["staff_days"]
        wd_assoc  = wd_row["nonstaff_days"]
    else:
        # Calculate once for each category
        import calendar as _cal
        from datetime import date as _dt
        wd_staff = sum(1 for d in range(1, _cal.monthrange(y,m)[1]+1)
                       if _dt(y,m,d).weekday() != 6)
        wd_assoc = _cal.monthrange(y, m)[1]

    # ── Bulk fetch ALL attendance in ONE query ───────────────
    all_att = conn.execute("""
        SELECT emp_code, status, is_half_day, ot_minutes, late_minutes
        FROM attendance
        WHERE strftime('%m', att_date)=? AND strftime('%Y', att_date)=?
    """, (f"{m:02d}", str(y))).fetchall()
    conn.close()

    # Group by emp_code in memory
    from collections import defaultdict
    att_by_emp = defaultdict(list)
    for r in all_att:
        att_by_emp[r["emp_code"]].append(r)

    rows = []
    for e in emps:
        ec  = e["emp_code"]
        wd  = wd_staff if e["category"] == "Staff" else wd_assoc
        att = att_by_emp.get(ec, [])
        present = sum(0.5 if r["is_half_day"] else 1 for r in att
                      if r["status"] not in ("Absent","Leave","WO","WOP","Holiday"))
        rows.append([
            ec, e["emp_name"], e["category"], e["department"] or "—", wd,
            round(present, 1),
            sum(1 for r in att if r["status"] == "Absent"),
            sum(1 for r in att if r["status"] == "Leave"),
            sum(1 for r in att if r["status"] == "WO"),
            sum(1 for r in att if r["status"] == "WOP"),
            sum(1 for r in att if r["status"] == "Holiday"),
            sum(1 for r in att if r["is_half_day"]),
            round(sum(r["ot_minutes"] or 0 for r in att) / 60, 2),
            sum(1 for r in att if (r["late_minutes"] or 0) > 0),
        ])

    hdrs = ["Emp Code","Name","Category","Dept","Working Days","Present","Absent",
            "Leave","WO","WOP","Holiday","Half Days","OT Hours","Late Days"]
    wb = make_wb("Attendance Summary", hdrs, rows,
                 [10,22,12,14,10,9,9,9,8,8,8,9,10,9], MONTHS[m-1], y, "10B981")
    return xlresp(wb, f"Attendance_{MONTHS[m-1]}_{y}.xlsx")

@app.route("/export/bank/<int:m>/<int:y>")
@amgr
def exp_bank(m,y):
    emp_code=request.args.get("emp_code","").strip()
    dept_f=request.args.get("dept","").strip()
    cat=request.args.get("cat","")
    cat_sql="" 
    if cat=="Staff": cat_sql=" AND e.category='Staff'"
    elif cat=="NonStaff": cat_sql=" AND e.category!='Staff'"
    conn=get_db()
    rows=conn.execute(f"""SELECT s.emp_code,e.emp_name,e.department,e.bank_account,e.bank_name,e.ifsc,s.net_salary
        FROM salary_records s JOIN employees e ON s.emp_code=e.emp_code
        WHERE s.month=? AND s.year=?{cat_sql} ORDER BY e.department,e.emp_name""",(m,y)).fetchall()
    conn.close()
    hdrs=["Emp Code","Name","Department","Bank Account","Bank Name","IFSC","Net Salary (Rs)"]
    wb=make_wb("Bank Transfer List",hdrs,[list(r) for r in rows],[10,24,16,20,16,14,16],MONTHS[m-1],y,"10B981")
    return xlresp(wb,f"BankTransfer_{MONTHS[m-1]}_{y}.xlsx")

@app.route("/export/pf/<int:m>/<int:y>")
@amgr
def exp_pf(m,y):
    emp_code=request.args.get("emp_code","").strip()
    dept_f=request.args.get("dept","").strip()
    cat=request.args.get("cat","")
    cat_sql="" 
    if cat=="Staff": cat_sql=" AND e.category='Staff'"
    elif cat=="NonStaff": cat_sql=" AND e.category!='Staff'"
    conn=get_db()
    rows=conn.execute(f"""SELECT s.emp_code,e.emp_name,e.department,e.pan,s.basic_earned,s.gross,
        s.pf,s.esi,s.tds,s.total_deductions,s.net_salary
        FROM salary_records s JOIN employees e ON s.emp_code=e.emp_code
        WHERE s.month=? AND s.year=?{cat_sql} ORDER BY e.department,e.emp_name""",(m,y)).fetchall()
    conn.close()
    hdrs=["Emp Code","Name","Department","PAN","Basic","Gross","PF 12%","ESI 0.75%","TDS","Total Ded","Net Salary"]
    wb=make_wb("PF & ESI Register",hdrs,[list(r) for r in rows],[10,22,16,14,14,14,14,14,14,14,14],MONTHS[m-1],y,"8B5CF6")
    return xlresp(wb,f"PF_ESI_{MONTHS[m-1]}_{y}.xlsx")

@app.route("/export/ot/<int:m>/<int:y>")
@amgr
def exp_ot(m,y):
    emp_code=request.args.get("emp_code","").strip()
    dept_f=request.args.get("dept","").strip()
    cat=request.args.get("cat","")
    cat_sql="" 
    if cat=="Staff": cat_sql=" AND e.category='Staff'"
    elif cat=="NonStaff": cat_sql=" AND e.category!='Staff'"
    conn=get_db()
    rows=conn.execute(f"""SELECT s.emp_code,e.emp_name,e.department,s.present_days,s.working_days,
        s.ot_hours,s.ot_amount,s.basic_earned,s.net_salary
        FROM salary_records s JOIN employees e ON s.emp_code=e.emp_code
        WHERE s.month=? AND s.year=?{cat_sql} 
        AND e.category='Associate'
        AND s.ot_hours>0
        ORDER BY s.ot_hours DESC""",(m,y)).fetchall()
    conn.close()
    hdrs=["Emp Code","Name","Department","Present Days","Working Days","OT Hours","OT Amount","Basic","Net Salary"]
    wb=make_wb("OT Report",hdrs,[list(r) for r in rows],[10,22,16,11,11,10,12,12,14],MONTHS[m-1],y,"F59E0B")
    return xlresp(wb,f"OT_Report_{MONTHS[m-1]}_{y}.xlsx")

@app.route("/export/yearly/<int:y>")
@amgr
def exp_yearly(y):
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from collections import defaultdict
    conn = get_db()

    # Fetch all attendance for the year in ONE query
    all_att = conn.execute("""
        SELECT strftime('%m', att_date) as mon, status, is_half_day
        FROM attendance
        WHERE strftime('%Y', att_date) = ?
    """, (str(y),)).fetchall()
    conn.close()

    # Group by month
    monthly = defaultdict(lambda: {"present":0,"absent":0,"wop":0,"hp":0,"leave":0,"half":0})
    for r in all_att:
        mon = int(r["mon"])
        st  = r["status"] or ""
        hd  = r["is_half_day"] or 0
        if st == "Present":
            monthly[mon]["present"] += 1
        elif st == "Absent":
            monthly[mon]["absent"] += 1
        elif st in ("WO","WOP") and r["status"] == "WOP":
            monthly[mon]["wop"] += 1
        elif st == "WOP":
            monthly[mon]["wop"] += 1
        elif st == "Holiday" and hd:
            monthly[mon]["hp"] += 1
        elif st == "Leave":
            monthly[mon]["leave"] += 1
        if hd and st == "Present":
            monthly[mon]["half"] += 1

    wb  = openpyxl.Workbook()
    ws  = wb.active
    ws.title = "Yearly Attendance"
    thin = Border(**{s: Side(style="thin", color="D0DCF0") for s in ["left","right","top","bottom"]})
    cl   = openpyxl.utils.get_column_letter

    hdrs = ["Month", "Present", "Absent", "WOP", "HP (Holiday Present)", "Leave", "Half Day"]
    ws.merge_cells(f"A1:{cl(len(hdrs))}1")
    ws["A1"] = f"{COMPANY} — Full Year Attendance Summary {y}"
    ws["A1"].font      = Font(bold=True, size=14, color="FFFFFF")
    ws["A1"].fill      = PatternFill("solid", fgColor="0052CC")
    ws["A1"].alignment = Alignment(horizontal="center")
    ws.row_dimensions[1].height = 30

    for i, h in enumerate(hdrs, 1):
        cell = ws.cell(row=2, column=i, value=h)
        cell.font      = Font(bold=True, size=11, color="FFFFFF")
        cell.fill      = PatternFill("solid", fgColor="1E2A40")
        cell.alignment = Alignment(horizontal="center")
        cell.border    = thin

    totals = [0] * 6
    for row_i, mon in enumerate(range(1, 13), 3):
        bg = "0E1422" if row_i % 2 == 0 else "121929"
        d  = monthly.get(mon, {})
        vals = [
            MONTHS[mon-1],
            d.get("present", 0),
            d.get("absent",  0),
            d.get("wop",     0),
            d.get("hp",      0),
            d.get("leave",   0),
            d.get("half",    0),
        ]
        for ci, val in enumerate(vals, 1):
            cell = ws.cell(row=row_i, column=ci, value=val)
            cell.fill      = PatternFill("solid", fgColor=bg)
            cell.border    = thin
            cell.font      = Font(size=11, color="E2E8F0")
            cell.alignment = Alignment(horizontal="center")
        for j, v in enumerate(vals[1:]):
            totals[j] += (v or 0)

    # Totals row
    sr = 15
    for ci in range(1, len(hdrs)+1):
        cell = ws.cell(row=sr, column=ci)
        cell.fill   = PatternFill("solid", fgColor="0052CC")
        cell.border = thin
    ws.cell(row=sr, column=1, value=f"TOTAL {y}").font = Font(bold=True, size=12, color="FFFFFF")
    ws.cell(row=sr, column=1).fill = PatternFill("solid", fgColor="0052CC")
    ws.cell(row=sr, column=1).alignment = Alignment(horizontal="center")
    for j, t in enumerate(totals, 2):
        cell = ws.cell(row=sr, column=j, value=t)
        cell.font      = Font(bold=True, size=12, color="FFFFFF")
        cell.fill      = PatternFill("solid", fgColor="0052CC")
        cell.alignment = Alignment(horizontal="center")

    widths = [18, 12, 12, 12, 22, 12, 12]
    for ci, w in enumerate(widths, 1):
        ws.column_dimensions[cl(ci)].width = w

    return xlresp(wb, f"Yearly_Attendance_{y}.xlsx")


# ─── LEAVE MANAGEMENT ────────────────────────────────


# ════════════════════════════════════════════════════════════
# LEAVE MANAGEMENT ROUTES
# ════════════════════════════════════════════════════════════

@app.route("/leaves/manage")
@amgr
def leaves_manage():
    """Admin leave management page — all in one"""
    conn = get_db()
    year = int(request.args.get("year", date.today().year))
    tab  = request.args.get("tab","requests")

    # Pending requests
    pending = conn.execute("""SELECT r.*, e.department, e.category
        FROM leave_requests r JOIN employees e ON r.emp_code=e.emp_code
        WHERE r.status='Pending' ORDER BY r.applied_on DESC""").fetchall()

    # All requests for year
    all_req = conn.execute("""SELECT r.*, e.department, e.category
        FROM leave_requests r JOIN employees e ON r.emp_code=e.emp_code
        WHERE strftime('%Y',r.from_date)=? ORDER BY r.applied_on DESC""",
        (str(year),)).fetchall()

    # Leave balances
    emps = conn.execute("""SELECT emp_code,emp_name,department,category
        FROM employees WHERE status='Active' AND category='Staff'
        ORDER BY department,emp_name""").fetchall()

    leave_types = conn.execute("SELECT * FROM leave_types WHERE is_active=1 ORDER BY code").fetchall()

    balances = {}
    for e in emps:
        bal = conn.execute("SELECT * FROM leave_balance WHERE emp_code=? AND year=?",
            (e["emp_code"], year)).fetchone()
        balances[e["emp_code"]] = dict(bal) if bal else {
            "cl_allotted":0,"el_allotted":0,"sl_allotted":0,
            "cl_used":0,"el_used":0,"sl_used":0,
            "cl_pending":0,"el_pending":0}

    # Summary counts
    summary = {
        "pending": len(pending),
        "approved": conn.execute("SELECT COUNT(*) FROM leave_requests WHERE status='Approved' AND strftime('%Y',from_date)=?", (str(year),)).fetchone()[0],
        "rejected": conn.execute("SELECT COUNT(*) FROM leave_requests WHERE status='Rejected' AND strftime('%Y',from_date)=?", (str(year),)).fetchone()[0],
    }

    conn.close()
    return render_template("leaves_manage.html",
        pending=[dict(r) for r in pending],
        all_req=[dict(r) for r in all_req],
        employees=[dict(e) for e in emps],
        leave_types=[dict(lt) for lt in leave_types],
        balances=balances,
        summary=summary,
        year=year, tab=tab, months=MONTHS,
        today=date.today().strftime("%Y-%m-%d"))

@app.route("/leaves/approved")
@amgr
def leaves_approved_report():
    """Approved leave report — department/category wise for managers"""
    conn = get_db()
    try:
        year  = int(request.args.get("year",  date.today().year))
        month = int(request.args.get("month", 0))
        dept  = request.args.get("dept", "")
        cat   = request.args.get("cat",  "")

        sql = """SELECT r.*, e.department, e.category, e.designation
            FROM leave_requests r JOIN employees e ON r.emp_code=e.emp_code
            WHERE r.status='Approved'
            AND strftime('%Y',r.from_date)=?"""
        params = [str(year)]
        if month:
            sql += " AND CAST(strftime('%m',r.from_date) AS INTEGER)=?"
            params.append(month)
        if dept: sql += " AND e.department=?"; params.append(dept)
        if cat:  sql += " AND e.category=?";   params.append(cat)
        sql += " ORDER BY e.department, e.category, r.from_date DESC"

        approved = conn.execute(sql, params).fetchall()

        dept_summary = conn.execute("""SELECT e.department, e.category,
            COUNT(*) as total_leaves, SUM(r.days) as total_days,
            COUNT(DISTINCT r.emp_code) as unique_employees
            FROM leave_requests r JOIN employees e ON r.emp_code=e.emp_code
            WHERE r.status='Approved' AND strftime('%Y',r.from_date)=?
            GROUP BY e.department, e.category ORDER BY e.department, e.category""",
            (str(year),)).fetchall()

        depts = [r["department"] for r in conn.execute(
            "SELECT DISTINCT department FROM employees WHERE status='Active' AND department IS NOT NULL ORDER BY department").fetchall()]

        return render_template("leaves_approved.html",
            approved=[dict(r) for r in approved],
            dept_summary=[dict(r) for r in dept_summary],
            depts=depts, months=MONTHS,
            year=year, month=month, dept=dept, cat=cat,
            total_count=len(approved),
            total_days=sum(r["days"] or 0 for r in approved))
    except Exception as e:
        return f"<h2>Error loading approved leave report</h2><p>{str(e)}</p><a href='/leaves/manage'>← Back</a>", 500
    finally:
        conn.close()


@app.route("/leaves/admin-assign", methods=["POST"])
@amgr
def leave_admin_assign():
    """Admin directly assigns leave to employee — auto approved"""
    d = request.json; conn = get_db()
    try:
        emp_code   = str(d.get("emp_code","")).strip()
        leave_type = str(d.get("leave_type","CL")).strip()
        from_date  = str(d.get("from_date","")).strip()
        to_date    = str(d.get("to_date", from_date)).strip()
        is_half    = 1 if d.get("is_half_day") else 0
        reason     = str(d.get("reason","Assigned by HR")).strip()
        remarks    = str(d.get("remarks","")).strip()
        deduct_bal = bool(d.get("deduct_balance", True))  # deduct from balance or not

        emp = conn.execute("SELECT * FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
        if not emp: return jsonify({"success":False,"error":f"Employee {emp_code} not found"})

        # Associate employees: only LWP or unpaid leaves
        if emp["category"] == "Associate" and leave_type not in ("LWP","CO"):
            leave_type = "LWP"  # Non-staff gets LWP

        # Calculate days
        if is_half:
            days = 0.5
        else:
            fd = datetime.strptime(from_date, "%Y-%m-%d").date()
            td = datetime.strptime(to_date, "%Y-%m-%d").date()
            days = max(1, (td - fd).days + 1)

        year = datetime.strptime(from_date, "%Y-%m-%d").year

        # Check if leave type is paid
        lt_row = conn.execute("SELECT is_paid FROM leave_types WHERE code=?", (leave_type,)).fetchone()
        is_paid = bool(lt_row["is_paid"]) if lt_row else (leave_type != "LWP")

        # Deduct from balance if applicable
        if deduct_bal and is_paid and leave_type in ("CL","EL","SL"):
            bal = conn.execute("SELECT * FROM leave_balance WHERE emp_code=? AND year=?",
                (emp_code, year)).fetchone()
            if bal:
                field = f"{leave_type.lower()}_allotted"
                used_field = f"{leave_type.lower()}_used"
                available = (bal[field] or 0) - (bal[used_field] or 0)
                # Allow admin override even if balance 0

        # Insert as Approved directly
        lid = conn.execute("""INSERT INTO leave_requests
            (emp_code,emp_name,leave_type,from_date,to_date,days,is_half_day,
             reason,status,applied_on,approved_by,approved_on,source,remarks)
            VALUES (?,?,?,?,?,?,?,?,'Approved',?,?,datetime('now'),'admin',?)""",
            (emp_code, emp["emp_name"], leave_type, from_date, to_date, days, is_half,
             reason, datetime.now().strftime("%Y-%m-%d %H:%M"),
             session.get("name","HR"), remarks)).lastrowid

        # Update balance
        if deduct_bal and leave_type in ("CL","EL","SL"):
            conn.execute("INSERT OR IGNORE INTO leave_balance (emp_code,year) VALUES (?,?)",
                (emp_code, year))
            conn.execute(f"""UPDATE leave_balance SET {leave_type.lower()}_used={leave_type.lower()}_used+?
                WHERE emp_code=? AND year=?""", (days, emp_code, year))

        # Mark attendance as Leave for each day
        fd = datetime.strptime(from_date, "%Y-%m-%d").date()
        td = datetime.strptime(to_date, "%Y-%m-%d").date()
        current = fd
        while current <= td:
            att_d = current.strftime("%Y-%m-%d")
            if is_half:
                # Half day: mark existing record
                existing = conn.execute("SELECT * FROM attendance WHERE emp_code=? AND att_date=?",
                    (emp_code, att_d)).fetchone()
                if existing:
                    conn.execute("UPDATE attendance SET is_half_day=1, status='Leave', remarks=? WHERE emp_code=? AND att_date=?",
                        (f"Leave: {leave_type} - {reason}", emp_code, att_d))
                else:
                    conn.execute("""INSERT OR REPLACE INTO attendance
                        (emp_code,att_date,in_time,out_time,working_minutes,status,
                         late_minutes,short_minutes,ot_minutes,is_half_day,remarks)
                        VALUES (?,?,'','',0,'Leave',0,0,0,1,?)""",
                        (emp_code, att_d, f"Leave: {leave_type} - {reason}"))
            else:
                status_att = "Leave" if is_paid else "Absent"
                conn.execute("""INSERT OR REPLACE INTO attendance
                    (emp_code,att_date,in_time,out_time,working_minutes,status,
                     late_minutes,short_minutes,ot_minutes,is_half_day,remarks)
                    VALUES (?,?,'','',0,?,0,0,0,0,?)""",
                    (emp_code, att_d, status_att, f"Leave: {leave_type} - {reason}"))
            current += timedelta(days=1)

        conn.commit()
        return jsonify({"success":True, "leave_id":lid,
                       "message":f"{days} day(s) {leave_type} assigned to {emp['emp_name']}"})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})
    finally: conn.close()

@app.route("/leaves/balance/init", methods=["POST"])
@amgr
def leave_balance_init():
    """Initialize/update leave balances for all staff employees for a year"""
    d = request.json; conn = get_db()
    try:
        year = int(d.get("year", date.today().year))
        ps = get_payroll_settings(conn)
        el_annual = float(ps.get("el_per_year", 16))
        cl_annual = float(ps.get("cl_per_year", 6))

        # Only Staff employees get EL/CL
        emps = conn.execute("""SELECT emp_code FROM employees
            WHERE status='Active' AND category='Staff'""").fetchall()

        initialized = 0
        for emp in emps:
            ec = emp["emp_code"]
            # Check if already exists
            existing = conn.execute("SELECT id,el_allotted,cl_allotted FROM leave_balance WHERE emp_code=? AND year=?",
                (ec, year)).fetchone()
            if existing:
                # Only update if 0 (don't overwrite manual settings)
                if not existing["el_allotted"]:
                    conn.execute("UPDATE leave_balance SET el_allotted=? WHERE emp_code=? AND year=?",
                        (el_annual, ec, year))
                if not existing["cl_allotted"]:
                    conn.execute("UPDATE leave_balance SET cl_allotted=? WHERE emp_code=? AND year=?",
                        (cl_annual, ec, year))
            else:
                conn.execute("""INSERT INTO leave_balance
                    (emp_code,year,el_allotted,cl_allotted,sl_allotted,cl_used,el_used,sl_used)
                    VALUES (?,?,?,?,6,0,0,0)""",
                    (ec, year, el_annual, cl_annual))
            initialized += 1

        conn.commit()
        return jsonify({"success":True,"initialized":initialized,
                       "message":f"Leave balance initialized for {initialized} Staff employees for {year}"})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})
    finally: conn.close()

@app.route("/leaves/balance/update", methods=["POST"])
@amgr
def leave_balance_update():
    """Manually update leave balance for one employee"""
    d = request.json; conn = get_db()
    try:
        ec   = d.get("emp_code")
        year = int(d.get("year", date.today().year))
        conn.execute("INSERT OR IGNORE INTO leave_balance (emp_code,year) VALUES (?,?)", (ec, year))
        for lt in ["cl","el","sl"]:
            for ftype in ["allotted","used"]:
                key = f"{lt}_{ftype}"
                if key in d:
                    conn.execute(f"UPDATE leave_balance SET {key}=? WHERE emp_code=? AND year=?",
                        (float(d[key]), ec, year))
        conn.commit()
        return jsonify({"success":True})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})
    finally: conn.close()

@app.route("/leaves/types", methods=["GET","POST"])
@amgr
def leave_types_manage():
    conn = get_db()
    if request.method == "POST":
        d = request.json
        if d.get("id"):
            conn.execute("""UPDATE leave_types SET name=?,is_paid=?,annual_quota=?,
                applicable_to=?,carry_forward=?,is_active=? WHERE id=?""",
                (d["name"],1 if d.get("is_paid") else 0,
                 float(d.get("annual_quota",0)),d.get("applicable_to","Staff"),
                 1 if d.get("carry_forward") else 0,
                 1 if d.get("is_active",True) else 0, d["id"]))
        else:
            conn.execute("""INSERT INTO leave_types (code,name,is_paid,annual_quota,applicable_to,carry_forward,is_active)
                VALUES (?,?,?,?,?,?,1)""",
                (d["code"],d["name"],1 if d.get("is_paid") else 0,
                 float(d.get("annual_quota",0)),d.get("applicable_to","Staff"),
                 1 if d.get("carry_forward") else 0))
        conn.commit(); conn.close()
        return jsonify({"success":True})
    types = conn.execute("SELECT * FROM leave_types ORDER BY code").fetchall()
    conn.close()
    return jsonify([dict(t) for t in types])

@app.route("/leaves/cancel/<int:lid>", methods=["POST"])
@amgr
def leave_cancel(lid):
    """Cancel an approved leave — restore balance and attendance"""
    conn = get_db()
    try:
        leave = conn.execute("SELECT * FROM leave_requests WHERE id=?", (lid,)).fetchone()
        if not leave: return jsonify({"success":False,"error":"Not found"})

        # Restore balance
        lt = leave["leave_type"].lower()
        if lt in ["cl","el","sl"] and leave["status"] == "Approved":
            year = datetime.strptime(leave["from_date"], "%Y-%m-%d").year
            conn.execute(f"""UPDATE leave_balance SET {lt}_used=MAX(0,{lt}_used-?)
                WHERE emp_code=? AND year=?""", (leave["days"], leave["emp_code"], year))

        # Remove attendance leave records
        fd = datetime.strptime(leave["from_date"], "%Y-%m-%d").date()
        td = datetime.strptime(leave["to_date"], "%Y-%m-%d").date()
        current = fd
        while current <= td:
            att_d = current.strftime("%Y-%m-%d")
            conn.execute("""UPDATE attendance SET status='Absent', is_half_day=0, remarks=NULL
                WHERE emp_code=? AND att_date=? AND status='Leave'""",
                (leave["emp_code"], att_d))
            current += timedelta(days=1)

        conn.execute("UPDATE leave_requests SET status='Cancelled' WHERE id=?", (lid,))
        conn.commit()
        return jsonify({"success":True})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})
    finally: conn.close()

@app.route("/leaves/get-balance/<emp_code>")
@amgr
def leave_get_balance(emp_code):
    """Get leave balance for an employee"""
    year = int(request.args.get("year", date.today().year))
    conn = get_db()
    bal = conn.execute("SELECT * FROM leave_balance WHERE emp_code=? AND year=?",
        (emp_code, year)).fetchone()
    pending = conn.execute("""SELECT leave_type, SUM(days) as pd FROM leave_requests
        WHERE emp_code=? AND status='Pending' AND strftime('%Y',from_date)=?
        GROUP BY leave_type""", (emp_code, str(year))).fetchall()
    conn.close()
    result = dict(bal) if bal else {"cl_allotted":0,"el_allotted":0,"sl_allotted":0,"cl_used":0,"el_used":0,"sl_used":0}
    result["pending"] = {r["leave_type"]: r["pd"] for r in pending}
    return jsonify(result)


# ════════════════════════════════════════════════════════════
# LEAVE MASTER SETTINGS
# ════════════════════════════════════════════════════════════

@app.route("/leave-master")
@amgr
def leave_master():
    """Leave Master Settings — types, earn rules, period"""
    conn = get_db()
    settings = conn.execute("SELECT * FROM leave_master_settings WHERE id=1").fetchone()
    leave_types = conn.execute("SELECT * FROM leave_types ORDER BY id").fetchall()
    conn.close()
    return render_template("leave_master.html",
        settings=dict(settings) if settings else {},
        leave_types=[dict(lt) for lt in leave_types],
        months=MONTHS)

@app.route("/leave-master/settings/save", methods=["POST"])
@amgr
def leave_master_settings_save():
    d = request.json; conn = get_db()
    try:
        conn.execute("""INSERT INTO leave_master_settings
            (id,period_type,financial_year_start_month,earn_auto_credit,updated_by,updated_on)
            VALUES (1,?,?,?,?,datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
            period_type=excluded.period_type,
            financial_year_start_month=excluded.financial_year_start_month,
            earn_auto_credit=excluded.earn_auto_credit,
            updated_by=excluded.updated_by,updated_on=excluded.updated_on""",
            (d.get("period_type","financial"),
             int(d.get("financial_year_start_month",4)),
             1 if d.get("earn_auto_credit") else 0,
             session.get("name","HR")))
        conn.commit(); conn.close()
        return jsonify({"success":True})
    except Exception as e:
        conn.close(); return jsonify({"success":False,"error":str(e)})

@app.route("/leave-master/type/save", methods=["POST"])
@amgr
def leave_type_save():
    """Save leave type with earn rules"""
    d = request.json; conn = get_db()
    try:
        lt_id = d.get("id")
        if lt_id:
            conn.execute("""UPDATE leave_types SET
                name=?,is_paid=?,annual_quota=?,applicable_to=?,
                carry_forward=?,is_active=?,earn_enabled=?,
                earn_every_n_days=?,earn_days_per_period=?,max_accumulation=?
                WHERE id=?""",
                (d["name"],1 if d.get("is_paid") else 0,
                 float(d.get("annual_quota",0)),d.get("applicable_to","Staff"),
                 1 if d.get("carry_forward") else 0,
                 1 if d.get("is_active",True) else 0,
                 1 if d.get("earn_enabled") else 0,
                 float(d.get("earn_every_n_days",30)),
                 float(d.get("earn_days_per_period",0.75)),
                 float(d.get("max_accumulation",0)),lt_id))
        else:
            conn.execute("""INSERT INTO leave_types
                (code,name,is_paid,annual_quota,applicable_to,carry_forward,is_active,
                 earn_enabled,earn_every_n_days,earn_days_per_period,max_accumulation)
                VALUES (?,?,?,?,?,?,1,?,?,?,?)""",
                (d["code"],d["name"],1 if d.get("is_paid") else 0,
                 float(d.get("annual_quota",0)),d.get("applicable_to","Staff"),
                 1 if d.get("carry_forward") else 0,
                 1 if d.get("earn_enabled") else 0,
                 float(d.get("earn_every_n_days",30)),
                 float(d.get("earn_days_per_period",0.75)),
                 float(d.get("max_accumulation",0))))
        conn.commit(); conn.close()
        return jsonify({"success":True})
    except Exception as e:
        conn.close(); return jsonify({"success":False,"error":str(e)})

@app.route("/leave-master/type/toggle/<int:lt_id>", methods=["POST"])
@amgr
def leave_type_toggle(lt_id):
    conn = get_db()
    conn.execute("UPDATE leave_types SET is_active=1-is_active WHERE id=?", (lt_id,))
    conn.commit(); conn.close()
    return jsonify({"success":True})

@app.route("/leave-master/balance/bulk-upload", methods=["POST"])
@amgr
def leave_balance_bulk_upload():
    """Bulk upload leave balances from Excel"""
    if "file" not in request.files:
        return jsonify({"success":False,"error":"No file"})
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(request.files["file"].read()))
        ws = wb.active
        hdrs = [str(c.value or "").strip().lower().replace(" ","_") for c in ws[1]]
        conn = get_db()
        done=0; errors=[]
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not any(row): continue
            d = dict(zip(hdrs, row))
            ec = str(d.get("emp_code","") or "").strip()
            if not ec: continue
            # Skip description/header rows (non-numeric year or emp_code)
            yr_raw = str(d.get("year","") or "").strip()
            try:
                yr = int(float(yr_raw))
            except (ValueError, TypeError):
                continue  # Skip rows like "Year e.g. 2026"
            if yr < 2000 or yr > 2099: continue  # Skip invalid years
            emp = conn.execute("SELECT emp_code FROM employees WHERE emp_code=?", (ec,)).fetchone()
            if not emp: errors.append(f"{ec}: not found"); continue
            cl  = float(d.get("cl_allotted",0) or 0)
            sl  = float(d.get("sl_allotted",0) or 0)
            el  = float(d.get("el_allotted",0) or 0)
            el_carry = float(d.get("el_carried",0) or 0)
            conn.execute("""INSERT INTO leave_balance
                (emp_code,year,cl_allotted,sl_allotted,el_allotted,el_carried)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(emp_code,year) DO UPDATE SET
                cl_allotted=excluded.cl_allotted,
                sl_allotted=excluded.sl_allotted,
                el_allotted=excluded.el_allotted,
                el_carried=excluded.el_carried""",
                (ec,yr,cl,sl,el,el_carry))
            done+=1
        conn.commit(); conn.close()
        return jsonify({"success":True,"done":done,"errors":errors[:10]})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})

@app.route("/leave-master/balance/template")
@amgr
def leave_balance_template():
    """Download bulk leave balance upload template"""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    wb = openpyxl.Workbook(); ws = wb.active
    ws.title = "Leave Balance Upload"
    hdrs  = ["emp_code","year","cl_allotted","sl_allotted","el_allotted","el_carried"]
    notes = ["Employee Code","Year e.g. 2026","Casual Leave Days","Sick Leave Days","Earned Leave Days","EL Carried Forward"]
    for ci,(h,n) in enumerate(zip(hdrs,notes),1):
        cell = ws.cell(1,ci,h)
        cell.font=Font(bold=True,color="FFFFFF")
        cell.fill=PatternFill("solid",fgColor="0052CC")
        cell.alignment=Alignment(horizontal="center")
        ws.cell(2,ci,n).font=Font(italic=True,color="888888",size=9)
    samples=[["1001",2026,6,6,16,0],["1002",2026,6,0,16,2]]
    for ri,row in enumerate(samples,3):
        for ci,v in enumerate(row,1): ws.cell(ri,ci,v)
    for i,w in enumerate([14,10,14,14,14,14],1):
        ws.column_dimensions[__import__("openpyxl").utils.get_column_letter(i)].width=w
    out=io.BytesIO(); wb.save(out); out.seek(0)
    return send_file(out,mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    as_attachment=True,download_name="Leave_Balance_Template.xlsx")

@app.route("/leaves")
@amgr
def leaves():
    conn = get_db()
    year = int(request.args.get("year", date.today().year))
    status_filter = request.args.get("status", "Pending")
    
    if status_filter == "all":
        requests = conn.execute("""SELECT r.*, e.department, e.category 
            FROM leave_requests r JOIN employees e ON r.emp_code=e.emp_code
            WHERE strftime('%Y', r.from_date)=? ORDER BY r.applied_on DESC""",
            (str(year),)).fetchall()
    else:
        requests = conn.execute("""SELECT r.*, e.department, e.category 
            FROM leave_requests r JOIN employees e ON r.emp_code=e.emp_code
            WHERE r.status=? AND strftime('%Y', r.from_date)=?
            ORDER BY r.applied_on DESC""",
            (status_filter, str(year))).fetchall()
    
    summary = conn.execute("""SELECT status, COUNT(*) as cnt 
        FROM leave_requests WHERE strftime('%Y', from_date)=?
        GROUP BY status""", (str(year),)).fetchall()
    conn.close()
    return render_template("leaves.html", requests=requests, 
        year=year, status_filter=status_filter, summary=summary)

@app.route("/leaves/apply", methods=["POST"])
@lreq
def apply_leave():
    d = request.json
    emp_code = session.get("emp_id") if session.get("role") == "employee" else d.get("emp_code")
    if not emp_code:
        return jsonify({"success": False, "error": "Employee code required"})
    conn = get_db()
    try:
        emp = conn.execute("SELECT emp_name FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
        if not emp: return jsonify({"success": False, "error": "Employee not found"})
        
        from_date = d.get("from_date")
        to_date   = d.get("to_date", from_date)
        is_half   = 1 if d.get("is_half_day") else 0
        # Employee does NOT select leave type — HR/Manager assigns it on approval
        # leave_type stored as "Pending" until approved
        leave_type = "Pending"

        # Calculate days
        if is_half:
            days = 0.5
        else:
            fd = datetime.strptime(from_date, "%Y-%m-%d").date()
            td = datetime.strptime(to_date, "%Y-%m-%d").date()
            days = (td - fd).days + 1

        conn.execute("""INSERT INTO leave_requests
            (emp_code, emp_name, leave_type, from_date, to_date, days, is_half_day, reason, status, applied_on)
            VALUES (?,?,?,?,?,?,?,?,'Pending',?)""",
            (emp_code, emp["emp_name"], leave_type, from_date, to_date, days, is_half,
             d.get("reason", ""), datetime.now().strftime("%Y-%m-%d %H:%M")))
        conn.commit()
        return jsonify({"success": True, "days": days})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
    finally:
        conn.close()

@app.route("/leaves/approve/<int:leave_id>", methods=["POST"])
@amgr
def approve_leave(leave_id):
    conn = get_db()
    d = request.json or {}
    try:
        leave = conn.execute("SELECT * FROM leave_requests WHERE id=?", (leave_id,)).fetchone()
        if not leave: return jsonify({"success": False, "error": "Not found"})

        # Manager/HR assigns the leave type at approval time
        assigned_type = d.get("leave_type") or leave["leave_type"]
        if assigned_type == "Pending" or not assigned_type:
            return jsonify({"success": False, "error": "Please select a Leave Type to assign"})

        conn.execute("""UPDATE leave_requests SET status='Approved', leave_type=?,
            approved_by=?, approved_on=? WHERE id=?""",
            (assigned_type, session.get("name", "HR"),
             datetime.now().strftime("%Y-%m-%d %H:%M"), leave_id))

        # Update leave balance
        year = datetime.strptime(leave["from_date"], "%Y-%m-%d").year
        lt = assigned_type.lower()
        if lt in ["cl", "sl", "el"]:
            conn.execute("""INSERT OR IGNORE INTO leave_balance (emp_code, year) VALUES (?,?)""",
                        (leave["emp_code"], year))
            conn.execute(f"""UPDATE leave_balance SET {lt}_used = {lt}_used + ?
                WHERE emp_code=? AND year=?""",
                (leave["days"], leave["emp_code"], year))
        
        # Mark attendance as Leave
        fd = datetime.strptime(leave["from_date"], "%Y-%m-%d").date()
        td = datetime.strptime(leave["to_date"], "%Y-%m-%d").date()
        emp = conn.execute("SELECT category FROM employees WHERE emp_code=?", (leave["emp_code"],)).fetchone()
        if emp:
            current = fd
            while current <= td:
                att_date = current.strftime("%Y-%m-%d")
                status = "Leave"
                if leave["is_half_day"]:
                    # Get existing record
                    existing = conn.execute("SELECT * FROM attendance WHERE emp_code=? AND att_date=?",
                                          (leave["emp_code"], att_date)).fetchone()
                    if existing:
                        conn.execute("UPDATE attendance SET is_half_day=1 WHERE emp_code=? AND att_date=?",
                                    (leave["emp_code"], att_date))
                    else:
                        conn.execute("""INSERT OR IGNORE INTO attendance 
                            (emp_code,att_date,status,is_half_day) VALUES (?,?,'Present',1)""",
                            (leave["emp_code"], att_date))
                else:
                    conn.execute("""INSERT OR REPLACE INTO attendance 
                        (emp_code,att_date,in_time,out_time,working_minutes,status,
                         late_minutes,short_minutes,ot_minutes,is_half_day)
                        VALUES (?,?,'','',0,?,0,0,0,0)""",
                        (leave["emp_code"], att_date, status))
                current += timedelta(days=1)
        
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
    finally:
        conn.close()

@app.route("/leaves/reject/<int:leave_id>", methods=["POST"])
@amgr
def reject_leave(leave_id):
    d = request.json
    conn = get_db()
    conn.execute("""UPDATE leave_requests SET status='Rejected',
        approved_by=?, approved_on=?, rejection_reason=? WHERE id=?""",
        (session.get("name","HR"), datetime.now().strftime("%Y-%m-%d %H:%M"),
         d.get("reason",""), leave_id))
    conn.commit(); conn.close()
    return jsonify({"success": True})

@app.route("/leaves/balance")
@amgr
def leave_balance():
    year = int(request.args.get("year", date.today().year))
    cat_filter = request.args.get("cat", "")
    conn = get_db()
    role     = session.get("role","")
    emp_id   = session.get("emp_id","")  # emp_code of logged-in user
    # Employee role: show only own leave balance
    if role == "employee" and emp_id:
        sql    = "SELECT emp_code, emp_name, department, category FROM employees WHERE emp_code=?"
        params = [emp_id]
    else:
        sql = "SELECT emp_code, emp_name, department, category FROM employees WHERE status='Active'"
        params = []
        if cat_filter:
            sql += " AND category=?"; params.append(cat_filter)
        sql += " ORDER BY category, emp_name"
    emps = conn.execute(sql, params).fetchall()
    balances = {}
    all_bal = conn.execute("SELECT * FROM leave_balance WHERE year=?", (year,)).fetchall()
    bal_map = {r["emp_code"]: dict(r) for r in all_bal}
    for e in emps:
        balances[e["emp_code"]] = bal_map.get(e["emp_code"], {
            "cl_allotted":0,"sl_allotted":0,"el_allotted":0,
            "cl_used":0,"sl_used":0,"el_used":0,"el_carried":0})
    active_lt = conn.execute("SELECT * FROM leave_types WHERE is_active=1 ORDER BY id").fetchall()
    lms = conn.execute("SELECT * FROM leave_master_settings WHERE id=1").fetchone()
    period_type = lms["period_type"] if lms else "financial"
    fy_start    = lms["financial_year_start_month"] if lms else 4
    if period_type == "financial":
        cur_m = date.today().month; cur_y = date.today().year
        period_label = f"Apr {cur_y} – Mar {cur_y+1}" if cur_m >= fy_start else f"Apr {cur_y-1} – Mar {cur_y}"
    else:
        period_label = f"Jan {year} – Dec {year}"
    # Monthly earn rates from payroll settings
    ps = get_payroll_settings(conn)
    el_per_year = float(ps.get("el_per_year", 16) or 16)
    cl_per_year = float(ps.get("cl_per_year", 6) or 6)
    el_monthly  = round(el_per_year / 12, 2)
    cl_monthly  = round(cl_per_year / 12, 2)
    conn.close()
    return render_template("leave_balance.html", employees=emps, balances=balances, year=year,
        active_leave_types=[dict(lt) for lt in active_lt],
        period_label=period_label, period_type=period_type,
        cat_filter=cat_filter, el_monthly=el_monthly, cl_monthly=cl_monthly)

@app.route("/leaves/earn-monthly", methods=["POST"])
@amgr
def leave_earn_monthly():
    """Credit monthly leave accrual based on Payroll Settings (EL/12 and CL/12 per month)"""
    d = request.json
    month = int(d.get("month", date.today().month))
    year  = int(d.get("year",  date.today().year))
    cat   = d.get("category", "")
    conn = get_db()
    try:
        ps = get_payroll_settings(conn)
        # Exact monthly = annual / 12, rounded to 4 decimal places
        el_per_year  = float(ps.get("el_per_year", 16) or 16)
        cl_per_year  = float(ps.get("cl_per_year", 6)  or 6)
        el_monthly   = round(el_per_year / 12, 4)   # e.g. 16/12 = 1.3333
        cl_monthly   = round(cl_per_year / 12, 4)   # e.g. 6/12  = 0.5

        sql = "SELECT emp_code, emp_name, category FROM employees WHERE status='Active'"
        params = []
        if cat: sql += " AND category=?"; params.append(cat)
        emps = conn.execute(sql, params).fetchall()

        credited = 0
        for e in emps:
            ec = e["emp_code"]
            conn.execute("INSERT OR IGNORE INTO leave_balance (emp_code,year) VALUES (?,?)", (ec, year))
            if e["category"] == "Staff":
                # Staff: both EL and CL
                conn.execute("""UPDATE leave_balance SET
                    el_allotted = ROUND(COALESCE(el_allotted,0) + ?, 4),
                    cl_allotted = ROUND(COALESCE(cl_allotted,0) + ?, 4)
                    WHERE emp_code=? AND year=?""", (el_monthly, cl_monthly, ec, year))
            else:
                # Associate: only EL (or as per company policy)
                conn.execute("""UPDATE leave_balance SET
                    el_allotted = ROUND(COALESCE(el_allotted,0) + ?, 4)
                    WHERE emp_code=? AND year=?""", (el_monthly, ec, year))
            credited += 1
        conn.commit()
        el_disp = f"{el_per_year}/{12} = {el_monthly:.4f}"
        cl_disp = f"{cl_per_year}/{12} = {cl_monthly:.4f}"
        return jsonify({"success": True, "credited": credited,
            "message": f"✅ Leave credited for {credited} employees — {MONTHS[month-1]} {year}\n"
                       f"EL: +{el_disp} days/head | CL (Staff): +{cl_disp} days/head"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
    finally: conn.close()


@app.route("/leaves/encash", methods=["POST"])
@amgr
def leave_encash():
    """Encash earned leaves for an employee"""
    d = request.json
    emp_code = str(d.get("emp_code","")).strip()
    year     = int(d.get("year", date.today().year))
    days     = float(d.get("days", 0))
    remarks  = str(d.get("remarks","Leave encashment")).strip()
    conn = get_db()
    try:
        emp = conn.execute("SELECT * FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
        if not emp: return jsonify({"success":False,"error":"Employee not found"})
        bal = conn.execute("SELECT * FROM leave_balance WHERE emp_code=? AND year=?",
                           (emp_code, year)).fetchone()
        if not bal: return jsonify({"success":False,"error":"No leave balance found for this year"})
        el_avail = (bal["el_allotted"] or 0) + (bal["el_carried"] or 0) - (bal["el_used"] or 0)
        if days > el_avail:
            return jsonify({"success":False,"error":f"Only {el_avail} EL days available for encashment"})
        # Calculate encashment amount
        basic = float(emp["basic"] or 0)
        per_day = basic / 26
        amount = round(per_day * days, 2)
        # Deduct from EL
        conn.execute("UPDATE leave_balance SET el_used=el_used+? WHERE emp_code=? AND year=?",
                     (days, emp_code, year))
        # Log in leave_requests
        conn.execute("""INSERT INTO leave_requests
            (emp_code,emp_name,leave_type,from_date,to_date,days,reason,status,
             applied_on,approved_by,approved_on,source,remarks)
            VALUES (?,?,'EL',?,?,?,?,'Approved',?,?,datetime('now'),'encashment',?)""",
            (emp_code, emp["emp_name"], date.today().isoformat(), date.today().isoformat(),
             days, remarks, datetime.now().strftime("%Y-%m-%d %H:%M"),
             session.get("name","HR"), f"Encashment: ₹{amount:,.2f} for {days} days. {remarks}"))
        conn.commit()
        return jsonify({"success":True,"amount":amount,"days":days,
            "message":f"✅ {days} EL days encashed = ₹{amount:,.2f} for {emp['emp_name']}"})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})
    finally: conn.close()


@app.route("/export/leave-balance")
@amgr
def export_leave_balance():
    """Export leave balance to Excel"""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    year = int(request.args.get("year", date.today().year))
    cat  = request.args.get("cat","")
    conn = get_db()
    sql = """SELECT e.emp_code, e.emp_name, e.department, e.category,
        COALESCE(b.cl_allotted,0) cl_allotted, COALESCE(b.cl_used,0) cl_used,
        COALESCE(b.sl_allotted,0) sl_allotted, COALESCE(b.sl_used,0) sl_used,
        COALESCE(b.el_allotted,0) el_allotted, COALESCE(b.el_used,0) el_used,
        COALESCE(b.el_carried,0) el_carried
        FROM employees e LEFT JOIN leave_balance b ON e.emp_code=b.emp_code AND b.year=?
        WHERE e.status='Active'"""
    params = [year]
    if cat: sql += " AND e.category=?"; params.append(cat)
    sql += " ORDER BY e.category, e.department, e.emp_name"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    wb = openpyxl.Workbook(); ws = wb.active
    ws.title = f"Leave Balance {year}"
    hdr_fill = PatternFill("solid", fgColor="003580")
    hdr_font = Font(bold=True, color="FFFFFF", size=10)
    thin = Border(*[Side(style="thin")]*4)
    # Title
    ws.merge_cells("A1:L1")
    t = ws["A1"]; t.value = f"Vijayshri Packaging Ltd. — Leave Balance Report {year}"
    t.font = Font(bold=True, size=13, color="003580"); t.alignment = Alignment(horizontal="center")
    ws.row_dimensions[1].height = 20
    # Headers
    hdrs = ["Code","Name","Dept","Category","CL Allotted","CL Used","CL Balance",
            "SL Allotted","SL Used","SL Balance","EL Allotted","EL Used","EL Carry","EL Balance"]
    ws.append([]); ws.append(hdrs)
    hr = ws.max_row
    for c, h in enumerate(hdrs, 1):
        cell = ws.cell(hr, c, h); cell.fill = hdr_fill; cell.font = hdr_font
        cell.alignment = Alignment(horizontal="center"); cell.border = thin
    alt = PatternFill("solid", fgColor="EBF5FF")
    for i, r in enumerate(rows):
        cl_bal = (r["cl_allotted"] or 0) - (r["cl_used"] or 0)
        sl_bal = (r["sl_allotted"] or 0) - (r["sl_used"] or 0)
        el_bal = (r["el_allotted"] or 0) + (r["el_carried"] or 0) - (r["el_used"] or 0)
        row_data = [r["emp_code"], r["emp_name"], r["department"], r["category"],
                    r["cl_allotted"], r["cl_used"], cl_bal,
                    r["sl_allotted"], r["sl_used"], sl_bal,
                    r["el_allotted"], r["el_used"], r["el_carried"], el_bal]
        ws.append(row_data)
        dr = ws.max_row
        fill = alt if i%2==0 else None
        for c in range(1, len(hdrs)+1):
            cell = ws.cell(dr, c); cell.border = thin
            if fill: cell.fill = fill
            if c > 4: cell.number_format = "0.00"
    for col, w in zip("ABCDEFGHIJKLMN", [8,22,14,10,10,8,10,10,8,10,10,8,8,10]):
        ws.column_dimensions[col].width = w
    from io import BytesIO
    buf = BytesIO(); wb.save(buf); buf.seek(0)
    fname = f"Leave_Balance_{cat or 'All'}_{year}.xlsx"
    return send_file(buf, as_attachment=True, download_name=fname,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/leaves/set-balance", methods=["POST"])
@amgr
def set_leave_balance():
    d = request.json
    conn = get_db()
    try:
        conn.execute("""INSERT INTO leave_balance (emp_code, year, cl_allotted, sl_allotted, el_allotted, el_carried)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(emp_code,year) DO UPDATE SET
            cl_allotted=excluded.cl_allotted,
            sl_allotted=excluded.sl_allotted,
            el_allotted=excluded.el_allotted,
            el_carried=excluded.el_carried""",
            (d["emp_code"], d["year"], float(d.get("cl",0)), float(d.get("sl",0)),
             float(d.get("el",0)), float(d.get("el_carried",0))))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
    finally:
        conn.close()

@app.route("/leaves/set-balance-all", methods=["POST"])
@amgr
def set_balance_all():
    """Set same leave allotment for all active employees"""
    d = request.json
    conn = get_db()
    try:
        year = int(d.get("year", date.today().year))
        cl = float(d.get("cl", 0))
        sl = float(d.get("sl", 0))
        el = float(d.get("el", 0))
        emps = conn.execute("SELECT emp_code FROM employees WHERE status='Active'").fetchall()
        for emp in emps:
            conn.execute("""INSERT INTO leave_balance (emp_code, year, cl_allotted, sl_allotted, el_allotted)
                VALUES (?,?,?,?,?)
                ON CONFLICT(emp_code,year) DO UPDATE SET
                cl_allotted=excluded.cl_allotted,
                sl_allotted=excluded.sl_allotted,
                el_allotted=excluded.el_allotted""",
                (emp["emp_code"], year, cl, sl, el))
        conn.commit()
        return jsonify({"success": True, "count": len(emps)})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
    finally:
        conn.close()

@app.route("/leaves/year-end", methods=["POST"])
@amgr
def year_end_process():
    """Carry forward or encash unused EL"""
    d = request.json
    year = int(d.get("year", date.today().year))
    action = d.get("action", "carry")  # carry or encash
    conn = get_db()
    try:
        balances = conn.execute("SELECT * FROM leave_balance WHERE year=?", (year,)).fetchall()
        processed = 0
        for bal in balances:
            unused_el = (bal["el_allotted"] or 0) + (bal["el_carried"] or 0) - (bal["el_used"] or 0)
            if unused_el <= 0: continue
            if action == "carry":
                # Add to next year balance
                conn.execute("""INSERT INTO leave_balance (emp_code, year, el_carried)
                    VALUES (?,?,?)
                    ON CONFLICT(emp_code,year) DO UPDATE SET el_carried=el_carried+excluded.el_carried""",
                    (bal["emp_code"], year+1, unused_el))
            elif action == "encash":
                # Calculate encashment amount
                emp = conn.execute("SELECT basic FROM employees WHERE emp_code=?", (bal["emp_code"],)).fetchone()
                if emp:
                    per_day = (emp["basic"] or 0) / 26
                    encash_amt = round(per_day * unused_el, 2)
                    # Add to salary record as bonus (informational)
                    conn.execute("""UPDATE leave_balance SET el_used=el_allotted+el_carried 
                        WHERE emp_code=? AND year=?""", (bal["emp_code"], year))
            processed += 1
        conn.commit()
        return jsonify({"success": True, "processed": processed})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
    finally:
        conn.close()

@app.route("/my-leaves")
@lreq
def my_leaves():
    emp_code = session.get("emp_id","")
    if not emp_code:
        return render_template("my_leaves.html", requests=[], balance=None, emp=None,
            year=date.today().year, error="No employee linked. Contact HR.")
    year = int(request.args.get("year", date.today().year))
    conn = get_db()
    requests = conn.execute("""SELECT * FROM leave_requests WHERE emp_code=?
        ORDER BY applied_on DESC LIMIT 50""", (emp_code,)).fetchall()
    bal = conn.execute("SELECT * FROM leave_balance WHERE emp_code=? AND year=?",
                      (emp_code, year)).fetchone()
    emp = conn.execute("SELECT * FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
    conn.close()
    return render_template("my_leaves.html", requests=requests, balance=bal, emp=emp,
        year=year, today_date=date.today().strftime("%Y-%m-%d"))



# ─── LETTERS ─────────────────────────────────────────



@app.route("/documents/log")
@amgr
def document_log_page():
    """View all generated documents with verification"""
    doc_type = request.args.get("type","")
    emp_q    = request.args.get("emp","")
    from_d   = request.args.get("from","")
    to_d     = request.args.get("to","")
    conn = get_db()
    sql = "SELECT * FROM document_log WHERE 1=1"
    params = []
    if doc_type: sql+=" AND doc_type=?"; params.append(doc_type)
    if emp_q:    sql+=" AND (emp_code LIKE ? OR emp_name LIKE ?)"; params+=[f"%{emp_q}%",f"%{emp_q}%"]
    if from_d:   sql+=" AND DATE(generated_on)>=?"; params.append(from_d)
    if to_d:     sql+=" AND DATE(generated_on)<=?"; params.append(to_d)
    sql += " ORDER BY generated_on DESC"
    docs = conn.execute(sql, params).fetchall()
    conn.close()
    return render_template("document_log.html", docs=docs,
        doc_type=doc_type, emp_q=emp_q, from_d=from_d, to_d=to_d,
        doc_types=["experience","relieving","increment","offer","warning","project_experience","resignation","payslip","appointment"])

@app.route("/documents/verify/<doc_code>")
def verify_document(doc_code):
    """Public verification — check if document code is genuine"""
    conn = get_db()
    doc = conn.execute("SELECT * FROM document_log WHERE doc_code=?", (doc_code,)).fetchone()
    conn.close()
    if doc:
        return jsonify({"verified":True,"doc_code":doc_code,
            "doc_type":doc["doc_type"],"emp_name":doc["emp_name"],
            "emp_code":doc["emp_code"],"generated_on":doc["generated_on"],
            "message":"✅ This is a genuine document issued by VIJAYSHRI PACKAGING LTD."})
    return jsonify({"verified":False,"doc_code":doc_code,
        "message":"❌ Document not found in records. May be forged."})

@app.route("/documents/log/export")
@amgr
def document_log_export():
    """Export document log to Excel"""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    doc_type = request.args.get("type","")
    conn = get_db()
    sql = "SELECT * FROM document_log WHERE 1=1"
    params = []
    if doc_type: sql+=" AND doc_type=?"; params.append(doc_type)
    sql += " ORDER BY generated_on DESC"
    docs = conn.execute(sql, params).fetchall()
    conn.close()
    wb = openpyxl.Workbook(); ws = wb.active
    ws.title = "Document Log"
    ws.merge_cells("A1:G1")
    ws["A1"] = f"VIJAYSHRI PACKAGING LTD. — Document Log"
    ws["A1"].font=Font(bold=True,size=11,color="FFFFFF")
    ws["A1"].fill=PatternFill("solid",fgColor="0052CC")
    ws["A1"].alignment=Alignment(horizontal="center")
    hdrs=["Doc Code","Type","Emp Code","Employee Name","Generated On","Generated By","Details"]
    for ci,h in enumerate(hdrs,1):
        cell=ws.cell(2,ci,h)
        cell.font=Font(bold=True,color="FFFFFF")
        cell.fill=PatternFill("solid",fgColor="1E3A5F")
    for d in docs:
        ws.append([d["doc_code"],d["doc_type"],d["emp_code"] or "",
                   d["emp_name"] or "",d["generated_on"] or "",
                   d["generated_by"] or "",d["details"] or ""])
    for i,w in enumerate([18,14,12,24,20,16,20],1):
        ws.column_dimensions[get_column_letter(i)].width=w
    return xlresp(wb,"Document_Log.xlsx")


@app.route("/masters/dept-ot-limits")
@amgr
def dept_ot_limits():
    conn = get_db()
    # Auto-seed departments from employees
    depts = conn.execute("SELECT DISTINCT department FROM employees WHERE status='Active' AND department IS NOT NULL ORDER BY department").fetchall()
    for d in depts:
        conn.execute("INSERT OR IGNORE INTO dept_ot_limits (department,monthly_ot_limit_hrs,per_day_hrs_per_emp) VALUES (?,50,2.5)", (d["department"],))
    conn.commit()
    # Add emp counts for each dept
    emp_counts = conn.execute("""SELECT department, COUNT(*) as cnt FROM employees
        WHERE status='Active' AND category='Associate' AND department IS NOT NULL GROUP BY department""").fetchall()
    emp_by_dept = {r["department"]: r["cnt"] for r in emp_counts}
    limits = conn.execute("SELECT * FROM dept_ot_limits ORDER BY department").fetchall()
    conn.close()
    return render_template("dept_ot_limits.html", limits=limits, emp_by_dept=emp_by_dept)

@app.route("/masters/dept-ot-limits/save", methods=["POST"])
@amgr
def save_dept_ot_limits():
    d = request.json
    conn = get_db()
    try:
        for item in d.get("limits",[]):
            conn.execute("""INSERT INTO dept_ot_limits (department,monthly_ot_limit_hrs,per_day_hrs_per_emp,alert_threshold_pct,updated_by,updated_on)
                VALUES (?,?,?,?,?,datetime('now'))
                ON CONFLICT(department) DO UPDATE SET
                monthly_ot_limit_hrs=excluded.monthly_ot_limit_hrs,
                per_day_hrs_per_emp=excluded.per_day_hrs_per_emp,
                alert_threshold_pct=excluded.alert_threshold_pct,
                updated_by=excluded.updated_by,updated_on=excluded.updated_on""",
                (item["department"],float(item.get("limit",50)),
                 float(item.get("per_day",2.5)),float(item.get("threshold",80)),
                 session.get("name","HR")))
        conn.commit(); conn.close()
        return jsonify({"success":True})
    except Exception as e:
        conn.close(); return jsonify({"success":False,"error":str(e)})


@app.route("/api/send-birthday-wishes", methods=["POST"])
@amgr
def send_birthday_wishes():
    """Send birthday/anniversary wishes via email + WhatsApp"""
    d = request.json
    emp_code = d.get("emp_code","")
    wish_type = d.get("type","birthday")  # birthday or anniversary
    conn = get_db()
    emp = conn.execute("SELECT * FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
    conn.close()
    if not emp: return jsonify({"success":False,"error":"Employee not found"})
    
    name = emp["emp_name"]
    results = []
    
    if wish_type == "birthday":
        subject = f"🎂 Happy Birthday {name}!"
        msg_text = f"🎂 *Happy Birthday {name}!* 🎉\n\nWishing you a wonderful birthday filled with joy and happiness!\n\n— Vijayshri Packaging Ltd. Family"
        html = f"""{make_email_header()}<div style="padding:24px;font-family:Arial;background:white;text-align:center;">
            <div style="font-size:48px;">🎂</div>
            <h2 style="color:#0052cc;">Happy Birthday, {name}!</h2>
            <p>Wishing you a wonderful birthday filled with joy and happiness!</p>
            <p style="color:#64748b;font-size:12px;">— Vijayshri Packaging Ltd. Family</p>
        </div>{make_email_footer()}"""
    else:
        subject = f"🎊 Happy Work Anniversary {name}!"
        msg_text = f"🎊 *Happy Work Anniversary {name}!* 🌟\n\nThank you for your valuable contributions!\n\n— Vijayshri Packaging Ltd."
        html = f"""{make_email_header()}<div style="padding:24px;font-family:Arial;background:white;text-align:center;">
            <div style="font-size:48px;">🎊</div>
            <h2 style="color:#0052cc;">Happy Work Anniversary, {name}!</h2>
            <p>Thank you for your valuable contributions to our company!</p>
        </div>{make_email_footer()}"""
    
    # Send email
    if emp.get("email"):
        ok, msg = send_email(emp["email"], subject, html, wish_type)
        results.append(f"Email: {'✅' if ok else '❌'} {msg}")
    
    # Send WhatsApp
    if emp.get("phone"):
        ok2, msg2 = send_whatsapp(emp["phone"], msg_text)
        results.append(f"WhatsApp: {'✅' if ok2 else '❌'} {msg2}")
    
    return jsonify({"success":True,"results":results})


@app.route("/api/dashboard-ot-alert")
@amgr  
def dashboard_ot_alert():
    """Dept OT analytics — limit = per_day_hrs × emp_count × working_days_so_far"""
    m = int(request.args.get("month", date.today().month))
    y = int(request.args.get("year",  date.today().year))
    from datetime import date as _dt
    today = _dt.today()
    yesterday = today - __import__("datetime").timedelta(days=1)
    conn = get_db()

    # Actual OT up to yesterday (Associate only — per business rule)
    ot_data = conn.execute("""
        SELECT e.department,
               SUM(a.ot_minutes) as total_ot_min,
               COUNT(DISTINCT a.emp_code) as emp_ot_count
        FROM attendance a
        JOIN employees e ON a.emp_code=e.emp_code
        WHERE strftime('%m',a.att_date)=? AND strftime('%Y',a.att_date)=?
        AND a.att_date <= ?
        AND e.status='Active' AND e.category='Associate' AND a.ot_minutes > 0
        GROUP BY e.department
    """, (f"{m:02d}", str(y), yesterday.strftime("%Y-%m-%d"))).fetchall()

    # Total active employees per department (Associate)
    emp_counts = conn.execute("""
        SELECT department, COUNT(*) as total_emp
        FROM employees WHERE status='Active' AND category='Associate' AND department IS NOT NULL
        GROUP BY department
    """).fetchall()
    emp_by_dept = {r["department"]: r["total_emp"] for r in emp_counts}

    # Working days elapsed in this month (up to yesterday, exclude Sundays)
    import calendar as _cal
    first_of_month = _dt(y, m, 1)
    days_elapsed = 0
    cur = first_of_month
    while cur <= yesterday and cur.month == m:
        if cur.weekday() != 6:  # not Sunday
            days_elapsed += 1
        cur += __import__("datetime").timedelta(days=1)

    limits = {r["department"]: r for r in conn.execute("SELECT * FROM dept_ot_limits").fetchall()}

    # All departments (even those with 0 OT)
    all_depts_data = {r["department"]: r for r in ot_data}

    result = []
    for dept, total_emp in sorted(emp_by_dept.items()):
        r = all_depts_data.get(dept)
        actual_hrs = round((r["total_ot_min"] or 0) / 60, 1) if r else 0
        limit_row  = limits.get(dept)
        per_day    = float(limit_row["per_day_hrs_per_emp"] if limit_row else 2.5)
        threshold  = float(limit_row["alert_threshold_pct"] if limit_row else 80)

        # Dynamic limit = per_day × emp_count × working_days_elapsed
        limit_hrs  = round(per_day * total_emp * days_elapsed, 1) if days_elapsed > 0 else per_day * total_emp
        pct        = round(actual_hrs / limit_hrs * 100, 1) if limit_hrs > 0 else 0
        alert      = pct >= threshold

        result.append({
            "department":   dept,
            "actual_hrs":   actual_hrs,
            "limit_hrs":    limit_hrs,
            "per_day_limit":per_day,
            "threshold_pct":threshold,
            "usage_pct":    pct,
            "emp_count":    total_emp,
            "days_elapsed": days_elapsed,
            "alert":        alert,
            "status": "🔴 Over Limit" if pct>=100 else ("🟡 Near Limit" if alert else "🟢 Normal")
        })

    result.sort(key=lambda x: x["usage_pct"], reverse=True)
    conn.close()
    return jsonify({"success":True,"data":result,"month":m,"year":y,"days_elapsed":days_elapsed})

def get_letter_settings_b64(conn):
    """Get letter settings with base64-encoded images. Fetches each column safely."""
    import base64 as _b64_ls
    result = {
        "has_header": False, "has_footer": False, "has_seal": False,
        "has_signature": False, "has_director_sign": False, "has_hr_sign": False,
        "header_b64": "", "footer_b64": "", "seal_b64": "",
        "sign_b64": "", "director_b64": "", "hr_b64": ""
    }
    # Ensure all columns exist (safe ALTER TABLE)
    for col in ["seal_image TEXT", "seal_filename TEXT",
                "signature_image BLOB", "signature_filename TEXT",
                "director_sign_image BLOB", "director_sign_filename TEXT",
                "hr_sign_image BLOB", "hr_sign_filename TEXT"]:
        try: conn.execute(f"ALTER TABLE letter_settings ADD COLUMN {col}")
        except: pass

    def _load(col, flag, b64key):
        try:
            row = conn.execute(f"SELECT {col} FROM letter_settings WHERE id=1").fetchone()
            if row and row[col]:
                data = bytes(row[col])
                if len(data) > 100:  # valid image, not empty/corrupt
                    result[flag] = True
                    result[b64key] = "data:image/png;base64," + _b64_ls.b64encode(data).decode()
        except Exception:
            pass

    _load("header_image",       "has_header",       "header_b64")
    _load("footer_image",       "has_footer",       "footer_b64")
    _load("seal_image",         "has_seal",         "seal_b64")
    _load("signature_image",    "has_signature",    "sign_b64")
    _load("director_sign_image","has_director_sign","director_b64")
    _load("hr_sign_image",      "has_hr_sign",      "hr_b64")
    return result


@app.route("/letters/settings", methods=["GET","POST"])
@amgr
def letter_settings():
    conn = get_db()
    if request.method == "POST":
        ftype = request.form.get("type")  # header, footer, or seal
        f = request.files.get("file")
        if f:
            data = f.read()
            fname = secure_filename(f.filename) if f.filename else "upload.png"
            # Ensure row 1 exists first
            conn.execute("INSERT OR IGNORE INTO letter_settings (id) VALUES (1)")
            if ftype == "header":
                conn.execute("UPDATE letter_settings SET header_image=?, header_filename=?, updated_on=datetime('now') WHERE id=1", (data, fname))
            elif ftype == "footer":
                conn.execute("UPDATE letter_settings SET footer_image=?, footer_filename=?, updated_on=datetime('now') WHERE id=1", (data, fname))
            elif ftype == "seal":
                conn.execute("UPDATE letter_settings SET seal_image=?, seal_filename=?, updated_on=datetime('now') WHERE id=1", (data, fname))
            elif ftype == "signature":
                conn.execute("UPDATE letter_settings SET signature_image=?, signature_filename=?, updated_on=datetime('now') WHERE id=1", (data, fname))
            elif ftype == "director_sign":
                conn.execute("UPDATE letter_settings SET director_sign_image=?, director_sign_filename=?, updated_on=datetime('now') WHERE id=1", (data, fname))
            elif ftype == "hr_sign":
                conn.execute("UPDATE letter_settings SET hr_sign_image=?, hr_sign_filename=?, updated_on=datetime('now') WHERE id=1", (data, fname))
            conn.commit()
        conn.close()
        return jsonify({"success": True})
    settings = conn.execute("SELECT header_filename,footer_filename,seal_filename,signature_filename,director_sign_filename,hr_sign_filename,updated_on FROM letter_settings WHERE id=1").fetchone()
    conn.close()
    return jsonify({"success":True,"settings":dict(settings) if settings else {}})

@app.route("/letters/settings/remove", methods=["POST"])
@amgr
def letter_settings_remove():
    """Remove a specific image (header/footer/seal/signature/director_sign/hr_sign)"""
    ftype = request.json.get("type","") if request.is_json else request.form.get("type","")
    col_map = {
        "header":       ("header_image", "header_filename"),
        "footer":       ("footer_image", "footer_filename"),
        "seal":         ("seal_image",   "seal_filename"),
        "signature":    ("signature_image", "signature_filename"),
        "director_sign":("director_sign_image","director_sign_filename"),
        "hr_sign":      ("hr_sign_image","hr_sign_filename"),
    }
    if ftype not in col_map:
        return jsonify({"success":False,"error":"Invalid type"})
    img_col, fn_col = col_map[ftype]
    conn = get_db()
    conn.execute(f"UPDATE letter_settings SET {img_col}=NULL, {fn_col}=NULL, updated_on=datetime('now') WHERE id=1")
    conn.commit()
    conn.close()
    return jsonify({"success":True})


@app.route("/letters/signature-image")
def letter_signature_img():
    conn = get_db()
    try:
        row = conn.execute("SELECT signature_image, signature_filename FROM letter_settings WHERE id=1").fetchone()
    except:
        row = None
    conn.close()
    if not row or not row["signature_image"]:
        import base64 as _b64; _epng = _b64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="); return send_file(io.BytesIO(_epng), mimetype="image/png")
    ext = row["signature_filename"].rsplit(".",1)[-1].lower() if row["signature_filename"] else "png"
    mime = "image/jpeg" if ext in ("jpg","jpeg") else "image/png"
    return send_file(io.BytesIO(row["signature_image"]), mimetype=mime)

@app.route("/letters/director-sign-image")
def letter_director_sign_img():
    conn = get_db()
    try:
        row = conn.execute("SELECT director_sign_image, director_sign_filename FROM letter_settings WHERE id=1").fetchone()
    except: row = None
    conn.close()
    if not row or not row["director_sign_image"]:
        import base64 as _b64; _epng = _b64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="); return send_file(io.BytesIO(_epng), mimetype="image/png")
    ext = row["director_sign_filename"].rsplit(".",1)[-1].lower() if row["director_sign_filename"] else "png"
    mime = "image/jpeg" if ext in ("jpg","jpeg") else "image/png"
    return send_file(io.BytesIO(row["director_sign_image"]), mimetype=mime)

@app.route("/letters/hr-sign-image")
def letter_hr_sign_img():
    conn = get_db()
    try:
        row = conn.execute("SELECT hr_sign_image, hr_sign_filename FROM letter_settings WHERE id=1").fetchone()
    except: row = None
    conn.close()
    if not row or not row["hr_sign_image"]:
        import base64 as _b64; _epng = _b64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="); return send_file(io.BytesIO(_epng), mimetype="image/png")
    ext = row["hr_sign_filename"].rsplit(".",1)[-1].lower() if row["hr_sign_filename"] else "png"
    mime = "image/jpeg" if ext in ("jpg","jpeg") else "image/png"
    return send_file(io.BytesIO(row["hr_sign_image"]), mimetype=mime)

@app.route("/letters/header-image")
def letter_header_img():
    conn = get_db()
    row = conn.execute("SELECT header_image,header_filename FROM letter_settings WHERE id=1").fetchone()
    conn.close()
    if not row or not row["header_image"]:
        import base64 as _b64; _epng = _b64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="); return send_file(io.BytesIO(_epng), mimetype="image/png")
    ext = row["header_filename"].rsplit(".",1)[-1].lower() if row["header_filename"] else "png"
    mime = "image/jpeg" if ext in ("jpg","jpeg") else "image/png"
    return send_file(io.BytesIO(row["header_image"]), mimetype=mime)

@app.route("/letters/seal-image")
def letter_seal_img():
    conn = get_db()
    try:
        row = conn.execute("SELECT seal_image, seal_filename FROM letter_settings WHERE id=1").fetchone()
    except:
        row = None
    conn.close()
    if not row or not row["seal_image"]:
        import base64 as _b64; _epng = _b64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="); return send_file(io.BytesIO(_epng), mimetype="image/png")
    ext = row["seal_filename"].rsplit(".",1)[-1].lower() if row["seal_filename"] else "png"
    mime = "image/jpeg" if ext in ("jpg","jpeg") else "image/png"
    return send_file(io.BytesIO(row["seal_image"]), mimetype=mime)

@app.route("/letters/footer-image")
def letter_footer_img():
    conn = get_db()
    row = conn.execute("SELECT footer_image,footer_filename FROM letter_settings WHERE id=1").fetchone()
    conn.close()
    if not row or not row["footer_image"]:
        import base64 as _b64; _epng = _b64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="); return send_file(io.BytesIO(_epng), mimetype="image/png")
    ext = row["footer_filename"].rsplit(".",1)[-1].lower() if row["footer_filename"] else "png"
    mime = "image/jpeg" if ext in ("jpg","jpeg") else "image/png"
    return send_file(io.BytesIO(row["footer_image"]), mimetype=mime)

def get_next_letter_code(letter_type, conn, emp_code="", emp_name=""):
    """Get next letter code with year + increment, log to document_log"""
    from datetime import date as _d
    yr = _d.today().year
    row = conn.execute("SELECT * FROM letter_counters WHERE letter_type=?", (letter_type,)).fetchone()
    if not row:
        pfx = letter_type[:3].upper()
        conn.execute("INSERT INTO letter_counters (letter_type,last_number,prefix) VALUES (?,1,?)", (letter_type, pfx))
        n = 1
    else:
        pfx = row["prefix"] or letter_type[:3].upper()
        n = (row["last_number"] or 0) + 1
        conn.execute("UPDATE letter_counters SET last_number=? WHERE letter_type=?", (n, letter_type))
    
    # Format: EXP-2026-0001
    doc_code = f"{pfx}-{yr}-{n:04d}"
    
    # Log to document_log
    try:
        conn.execute("""INSERT OR IGNORE INTO document_log
            (doc_code, doc_type, emp_code, emp_name, generated_on, generated_by)
            VALUES (?,?,?,?,?,?)""",
            (doc_code, letter_type, emp_code, emp_name, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), session.get("name","System")))
    except: pass
    
    return doc_code

@app.route("/letters")
@amgr
def letters():
    conn = get_db()
    emps = conn.execute("SELECT emp_code, emp_name, department, designation, category, status FROM employees ORDER BY emp_name").fetchall()
    conn.close()
    return render_template("letters.html", employees=emps, has_warning_letter=True)

@app.route("/letters/offer/<emp_code>")
@amgr
def offer_letter(emp_code):
    conn = get_db()
    emp = conn.execute("SELECT * FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
    if not emp: conn.close(); return "Employee not found", 404
    letter_code = get_next_letter_code("offer", conn, emp["emp_code"], emp["emp_name"])
    _ls = get_letter_settings_b64(conn)
    has_header       = _ls["has_header"]
    has_footer       = _ls["has_footer"]
    has_seal         = _ls["has_seal"]
    has_signature    = _ls["has_signature"]
    has_director_sign= _ls["has_director_sign"]
    has_hr_sign      = _ls["has_hr_sign"]
    sign_b64     = _ls["sign_b64"]
    seal_b64     = _ls["seal_b64"]
    header_b64   = _ls["header_b64"]
    footer_b64   = _ls["footer_b64"]
    director_b64 = _ls["director_b64"]
    hr_b64       = _ls["hr_b64"]
    conn.commit(); conn.close()
    custom_lines = request.args.get("custom_lines", "").strip()
    return render_template("offer_letter.html", emp=dict(emp), company=COMPANY,
        today=date.today().strftime("%d %B %Y"), custom_lines=custom_lines,
        letter_code=letter_code,
        has_header=has_header, has_footer=has_footer, has_seal=has_seal, has_signature=has_signature,
        sign_b64=sign_b64, seal_b64=seal_b64, header_b64=header_b64, footer_b64=footer_b64,
        director_b64=director_b64, hr_b64=hr_b64,
        has_director_sign=has_director_sign, has_hr_sign=has_hr_sign)

@app.route("/letters/offer-data", methods=["POST"])
@amgr
def offer_letter_data():
    """Get employee data for offer letter preview"""
    d = request.json
    emp_code = d.get("emp_code")
    conn = get_db()
    emp = conn.execute("SELECT * FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
    conn.close()
    if not emp: return jsonify({"success": False, "error": "Not found"})
    emp_d = dict(emp)
    # Calculate salary breakup
    basic = emp_d.get("basic", 0) or 0
    hra = emp_d.get("hra", 0) or 0
    special = emp_d.get("special_allowance", 0) or 0
    gross = basic + hra + special
    pf = round(basic * 0.12, 2) if emp_d.get("pf_applicable") else 0
    esi = round(gross * 0.0075, 2) if emp_d.get("esi_applicable") and gross <= 21000 else 0
    emp_d["gross"] = gross
    emp_d["pf_amount"] = pf
    emp_d["esi_amount"] = esi
    emp_d["ctc"] = round(gross + pf, 2)
    return jsonify({"success": True, "emp": emp_d})

@app.route("/letters/resignation", methods=["GET"])
@lreq
def resignation_form():
    """Both employee and HR can access this"""
    emp_code = session.get("emp_id") if session.get("role") == "employee" else request.args.get("emp_code", "")
    conn = get_db()
    emp = conn.execute("SELECT * FROM employees WHERE emp_code=?", (emp_code,)).fetchone() if emp_code else None
    emps = conn.execute("SELECT emp_code, emp_name, department FROM employees WHERE status='Active' ORDER BY emp_name").fetchall()
    conn.close()
    return render_template("resignation.html", emp=dict(emp) if emp else None,
        employees=emps, today=date.today().strftime("%Y-%m-%d"))

@app.route("/letters/resignation/submit", methods=["POST"])
@lreq
def submit_resignation():
    d = request.json
    emp_code = session.get("emp_id") if session.get("role") == "employee" else d.get("emp_code")
    conn = get_db()
    try:
        emp = conn.execute("SELECT emp_name FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
        if not emp: return jsonify({"success": False, "error": "Employee not found"})
        # Save resignation details
        conn.execute("""UPDATE employees SET resignation_date=?, exit_reason=? WHERE emp_code=?""",
            (d.get("resignation_date", date.today().strftime("%Y-%m-%d")),
             d.get("reason", "Personal Reasons"), emp_code))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        conn.close()
        return jsonify({"success": False, "error": str(e)})



@app.route("/letters/resignation-print/<emp_code>")
@amgr
def resignation_letter_print(emp_code):
    """Printable resignation acceptance letter"""
    conn = get_db()
    emp = conn.execute("SELECT * FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
    if not emp: conn.close(); return "Employee not found", 404
    letter_code = get_next_letter_code("resignation", conn, emp["emp_code"], emp["emp_name"])
    _ls = get_letter_settings_b64(conn)
    has_header       = _ls["has_header"]
    has_footer       = _ls["has_footer"]
    has_seal         = _ls["has_seal"]
    has_signature    = _ls["has_signature"]
    has_director_sign= _ls["has_director_sign"]
    has_hr_sign      = _ls["has_hr_sign"]
    sign_b64     = _ls["sign_b64"]
    seal_b64     = _ls["seal_b64"]
    header_b64   = _ls["header_b64"]
    footer_b64   = _ls["footer_b64"]
    director_b64 = _ls["director_b64"]
    hr_b64       = _ls["hr_b64"]
    conn.commit(); conn.close()
    custom_lines = request.args.get("custom_lines", "").strip()
    resignation_date = request.args.get("resignation_date", date.today().strftime("%Y-%m-%d"))
    last_working_day = request.args.get("last_working_day", "")
    return render_template("resignation_letter.html", emp=dict(emp),
        company=COMPANY, today=date.today().strftime("%d %B %Y"),
        letter_code=letter_code, custom_lines=custom_lines,
        resignation_date=resignation_date, last_working_day=last_working_day,
        has_header=has_header, has_footer=has_footer,
        has_seal=has_seal, has_signature=has_signature,
        sign_b64=sign_b64, seal_b64=seal_b64, header_b64=header_b64, footer_b64=footer_b64,
        director_b64=director_b64, hr_b64=hr_b64,
        has_director_sign=has_director_sign, has_hr_sign=has_hr_sign)

@app.route("/export/dept-history")
@amgr
def export_dept_history():
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    conn = get_db()
    rows = conn.execute("""
        SELECT h.emp_code, h.emp_name, h.old_department, h.new_department,
               h.changed_on, h.changed_by,
               -- Duration: calculate days in old dept
               CAST(julianday(h.changed_on) - COALESCE(
                   (SELECT julianday(h2.changed_on)
                    FROM emp_dept_history h2
                    WHERE h2.emp_code = h.emp_code AND h2.changed_on < h.changed_on
                    ORDER BY h2.changed_on DESC LIMIT 1),
                   julianday((SELECT date_of_joining FROM employees WHERE emp_code=h.emp_code))
               ) AS INTEGER) as days_in_old_dept
        FROM emp_dept_history h
        ORDER BY h.emp_code, h.changed_on
    """).fetchall()
    # Also get current dept for each employee to add "current" row
    current_depts = {r["emp_code"]: r for r in conn.execute(
        "SELECT emp_code, emp_name, department, date_of_joining FROM employees").fetchall()}
    conn.close()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Department Change History"

    # Header style
    hdr_fill = PatternFill("solid", fgColor="003580")
    hdr_font = Font(bold=True, color="FFFFFF", size=11)
    hdr_align = Alignment(horizontal="center", vertical="center", wrap_text=True)
    thin = Border(
        left=Side(style="thin"), right=Side(style="thin"),
        top=Side(style="thin"), bottom=Side(style="thin"))

    # Title row
    ws.merge_cells("A1:H1")
    t = ws["A1"]
    t.value = "Vijayshri Packaging Ltd. — Employee Department Change History Report"
    t.font = Font(bold=True, size=13, color="003580")
    t.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 22

    # Sub-title
    ws.merge_cells("A2:H2")
    ws["A2"].value = f"Generated on: {datetime.now().strftime('%d-%b-%Y %H:%M')}"
    ws["A2"].font = Font(italic=True, size=10, color="666666")
    ws["A2"].alignment = Alignment(horizontal="center")

    # Column headers
    headers = ["Emp Code","Employee Name","Previous Department","New Department",
               "Changed On","Changed By","Days in Previous Dept","Remarks"]
    ws.append([])  # blank row
    ws.append(headers)
    hdr_row = ws.max_row
    for col_idx, h in enumerate(headers, 1):
        cell = ws.cell(row=hdr_row, column=col_idx)
        cell.fill = hdr_fill; cell.font = hdr_font
        cell.alignment = hdr_align; cell.border = thin

    # Data rows
    alt_fill = PatternFill("solid", fgColor="EBF5FF")
    for i, r in enumerate(rows):
        days = r["days_in_old_dept"] or 0
        if days > 0:
            duration = f"{days} days"
            if days >= 365:
                yrs = days // 365; mos = (days % 365) // 30
                duration = f"{yrs}y {mos}m ({days} days)"
            elif days >= 30:
                mos = days // 30
                duration = f"{mos}m ({days} days)"
        else:
            duration = "—"

        row_data = [r["emp_code"], r["emp_name"], r["old_department"] or "—",
                    r["new_department"], r["changed_on"][:16] if r["changed_on"] else "—",
                    r["changed_by"] or "Admin", duration, "Department Transfer"]
        ws.append(row_data)
        data_row = ws.max_row
        fill = alt_fill if i % 2 == 0 else None
        for col_idx in range(1, len(headers)+1):
            cell = ws.cell(row=data_row, column=col_idx)
            if fill: cell.fill = fill
            cell.border = thin
            cell.alignment = Alignment(vertical="center")
            if col_idx == 1:
                cell.font = Font(bold=True, color="0052CC")

    # Column widths
    for col, width in zip("ABCDEFGH", [10,22,24,24,17,14,20,18]):
        ws.column_dimensions[col].width = width
    ws.row_dimensions[hdr_row].height = 18

    from io import BytesIO
    buf = BytesIO()
    wb.save(buf); buf.seek(0)
    fname = f"Dept_Change_History_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
    return send_file(buf, as_attachment=True, download_name=fname,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/export/employees")
@amgr
def exp_employees():
    cat = request.args.get("cat","")
    import openpyxl
    from openpyxl.styles import Font,PatternFill,Alignment,Border,Side
    conn = get_db()
    sql = "SELECT * FROM employees WHERE status='Active'"
    if cat == "Staff":    sql += " AND category='Staff'"
    elif cat == "NonStaff": sql += " AND category!='Staff'"
    sql += " ORDER BY category,emp_name"
    emps = conn.execute(sql).fetchall()
    conn.close()
    conn2 = get_db()  # for scheme lookups
    thin = Border(**{s:Side(style="thin",color="D0DCF0") for s in ["left","right","top","bottom"]})
    wb = openpyxl.Workbook(); ws = wb.active
    label = "Staff Only" if cat=="Staff" else ("Associate Only" if cat=="NonStaff" else "All Employees")
    ws.title = label
    base_hdrs = ["Emp Code","Name","Category","Department","Designation","Location","Gender","DOB","DOJ","Weekly Off","Basic","HRA","Special Allow","Gross","PF Applicable","ESIC Applicable","TDS%","Scheme","Phone","Official Email","Personal Email","Bank Account","Bank Name","IFSC","PAN","Aadhar","UAN Number","PF Number","ESIC Number","Status"]
    base_widths = [10,24,12,16,18,12,8,12,12,10,10,10,12,10,6,6,6,14,14,20,16,14,18,14,14,14,16,16,16,10]
    # Fetch custom fields and append to headers
    try:
        custom_fields_export = conn2.execute(
            "SELECT field_name,field_label FROM employee_custom_fields WHERE is_active=1 AND in_export=1 ORDER BY display_order,field_label"
        ).fetchall()
    except:
        custom_fields_export = []
    hdrs   = base_hdrs   + [cf["field_label"] for cf in custom_fields_export]
    widths = base_widths + [16]*len(custom_fields_export)
    ws.merge_cells(f"A1:{openpyxl.utils.get_column_letter(len(hdrs))}1")
    ws["A1"] = f"Vijayshri Packaging Ltd. — Employee List ({label})"
    ws["A1"].font = Font(bold=True,size=13,color="FFFFFF",name="Arial")
    ws["A1"].fill = PatternFill("solid",fgColor="0052CC")
    ws["A1"].alignment = Alignment(horizontal="center")
    ws.row_dimensions[1].height = 26
    for ci,h in enumerate(hdrs,1):
        c = ws.cell(row=2,column=ci,value=h)
        c.font = Font(bold=True,size=10,color="FFFFFF",name="Arial")
        c.fill = PatternFill("solid",fgColor="1E2A40")
        c.alignment = Alignment(horizontal="center",wrap_text=True)
        c.border = thin
    ws.row_dimensions[2].height = 22
    for ci,w in enumerate(widths,1):
        ws.column_dimensions[openpyxl.utils.get_column_letter(ci)].width = w
    for ri,e in enumerate(emps,3):
        bg = "0E1422" if ri%2==0 else "121929"
        gross = (e["basic"] or 0)+(e["hra"] or 0)+(e["special_allowance"] or 0)
        # Get scheme name
        scheme_name = ""
        if e["scheme_id"]:
            sr = conn2.execute("SELECT scheme_name FROM employee_schemes WHERE id=?", (e["scheme_id"],)).fetchone()
            scheme_name = sr["scheme_name"] if sr else ""
        # Get custom field values for this employee
        custom_vals_row = []
        for cf in custom_fields_export:
            cv = conn2.execute("SELECT field_value FROM employee_custom_values WHERE emp_code=? AND field_name=?",
                               (e["emp_code"], cf["field_name"])).fetchone()
            custom_vals_row.append(cv["field_value"] if cv and cv["field_value"] else "")

        vals = [e["emp_code"],e["emp_name"],e["category"],e["department"] or "",
                e["designation"] or "",e["location"] or "",e["gender"] or "",
                e["date_of_birth"] or "",e["date_of_joining"] or "",
                e["weekly_off"] if "weekly_off" in e.keys() else "Sunday",
                e["basic"] or 0,e["hra"] or 0,e["special_allowance"] or 0,gross,
                "Yes" if e["pf_applicable"] else "No",
                "Yes" if e["esi_applicable"] else "No",
                e["tds_percent"] or 0,scheme_name,
                e["phone"] or "",
                e["official_email"] or e["email"] or "",
                e["personal_email"] or "",
                e["bank_account"] or "",e["bank_name"] or "",e["ifsc"] or "",
                e["pan"] or "",e["aadhar"] or "",
                e["uan_number"] or "",e["pf_number"] or "",e["esic_number"] or "",
                e["status"]] + custom_vals_row
        for ci,val in enumerate(vals,1):
            c = ws.cell(row=ri,column=ci,value=val)
            c.fill = PatternFill("solid",fgColor=bg)
            c.border = thin
            c.font = Font(size=10,color="E2E8F0",name="Arial")
            c.alignment = Alignment(horizontal="left" if ci==2 else "center")
    conn2.close()
    out = io.BytesIO(); wb.save(out); out.seek(0)
    fname = f"Employees_{label.replace(' ','_')}.xlsx"
    return send_file(out,mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    as_attachment=True,download_name=fname)


# ─── EMAIL SYSTEM ────────────────────────────────────

def get_email_settings():
    conn = get_db()
    s = conn.execute("SELECT * FROM email_settings WHERE id=1").fetchone()
    conn.close()
    return dict(s) if s else {}


def send_whatsapp(phone, message):
    """Send WhatsApp message via configured API (e.g., Twilio/UltraMsg/WA Business API)"""
    import requests as _req
    conn = get_db()
    s = conn.execute("SELECT * FROM email_settings WHERE id=1").fetchone()
    conn.close()
    if not s or not s["whatsapp_enabled"] or not s["whatsapp_api_url"]:
        return False, "WhatsApp not configured"
    try:
        # Support UltraMsg / CallMeBot / Twilio style APIs
        api_url = s["whatsapp_api_url"].strip()
        api_key = s["whatsapp_api_key"].strip()
        # Format phone: remove spaces, add +91 if needed
        ph = str(phone).strip().replace(" ","").replace("-","")
        if not ph.startswith("+"): ph = "+91" + ph.lstrip("0")
        
        payload = {"token": api_key, "to": ph, "body": message}
        r = _req.post(api_url, data=payload, timeout=10)
        if r.status_code == 200:
            return True, "WhatsApp sent"
        return False, f"API error: {r.text[:200]}"
    except Exception as e:
        return False, str(e)

def send_email(to_email, subject, html_body, email_type="general"):
    """Send email using configured SMTP settings.
    Supports Gmail, Outlook (App Password), custom SMTP.
    Port 465 = SSL direct. Port 587 = STARTTLS.
    """
    import smtplib, ssl
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    settings = get_email_settings()
    if not settings.get("is_active") or not settings.get("email") or not settings.get("password"):
        return False, "Email not configured. Go to Settings → Email Settings."

    try:
        msg = MIMEMultipart("alternative")
        msg["From"] = f"{settings['sender_name']} <{settings['email']}>"
        msg["To"]   = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(html_body, "html"))

        host = settings["smtp_host"]
        port = int(settings.get("smtp_port", 587))

        if port == 465:
            # Direct SSL
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(host, port, context=ctx) as server:
                server.login(settings["email"], settings["password"])
                server.sendmail(settings["email"], to_email, msg.as_string())
        else:
            # STARTTLS (587)
            with smtplib.SMTP(host, port, timeout=30) as server:
                server.ehlo()
                server.starttls()
                server.ehlo()
                server.login(settings["email"], settings["password"])
                server.sendmail(settings["email"], to_email, msg.as_string())

        conn = get_db()
        conn.execute("INSERT INTO email_logs (to_email,subject,type,status,sent_on) VALUES (?,?,?,?,?)",
                    (to_email, subject, email_type, "Sent", datetime.now().strftime("%Y-%m-%d %H:%M")))
        conn.commit(); conn.close()
        return True, "Email sent successfully!"
    except Exception as e:
        err = str(e)
        # Friendly message for Outlook SMTP Auth disabled
        if "535" in err and "SmtpClientAuthentication" in err:
            err = ("Microsoft Outlook SMTP Auth is DISABLED on your tenant. Solutions:\n"
                   "Option 1 (Easiest): Use GMAIL instead — create a Gmail account for company notifications, "
                   "enable 2FA, generate App Password at myaccount.google.com/apppasswords\n"
                   "Option 2: Enable SMTP AUTH in Microsoft 365 Admin Center → "
                   "Settings → Org settings → Modern authentication → enable SMTP AUTH for your user\n"
                   "Option 3: Use smtp.office365.com port 587 with App Password "
                   "(requires enabling legacy auth in Exchange Admin Center)")
        try:
            conn = get_db()
            conn.execute("INSERT INTO email_logs (to_email,subject,type,status,sent_on,error) VALUES (?,?,?,?,?,?)",
                        (to_email, subject, email_type, "Failed", datetime.now().strftime("%Y-%m-%d %H:%M"), err[:500]))
            conn.commit(); conn.close()
        except: pass
        return False, err

def make_email_header():
    return """
    <div style="background:linear-gradient(135deg,#0052cc,#0096dc);padding:24px;text-align:center;border-radius:12px 12px 0 0;">
        <div style="font-size:22px;font-weight:800;color:white;font-family:Arial,sans-serif;">Vijayshri Packaging Ltd.</div>
        <div style="font-size:12px;color:rgba(255,255,255,.8);margin-top:4px;">An End-to-end Packaging Solution</div>
    </div>"""

def make_email_footer():
    return """
    <div style="background:#f0f4f8;padding:16px;text-align:center;border-radius:0 0 12px 12px;font-size:11px;color:#64748b;font-family:Arial,sans-serif;">
        This is an automated email from Vijayshri Packaging Ltd. PayRoll System.<br>
        Please do not reply to this email.
    </div>"""

# ─── EMAIL ROUTES ────────────────────────────────────

@app.route("/email-settings", methods=["GET","POST"])
@amgr
def email_settings():
    conn = get_db()
    if request.method == "POST":
        d = request.form
        provider = d.get("provider","gmail")
        if provider == "gmail":
            host = "smtp.gmail.com"; port = 587
        elif provider == "outlook":
            host = "smtp.office365.com"; port = 587
        else:
            host = d.get("smtp_host","smtp.gmail.com")
            port = int(d.get("smtp_port",587))
        conn.execute("""UPDATE email_settings SET provider=?,smtp_host=?,smtp_port=?,
            email=?,password=?,sender_name=?,is_active=? WHERE id=1""",
            (provider,host,port,d.get("email",""),d.get("password",""),
             d.get("sender_name","Vijayshri Packaging Ltd."),
             1 if d.get("is_active") else 0))
        conn.commit()
        flash_msg = "Email settings saved!"
        settings = conn.execute("SELECT * FROM email_settings WHERE id=1").fetchone()
        logs = conn.execute("SELECT * FROM email_logs ORDER BY id DESC LIMIT 20").fetchall()
        conn.close()
        return render_template("email_settings.html", settings=dict(settings), logs=logs, msg=flash_msg,
            today_month=date.today().month, today_year=date.today().year, enumerate=enumerate)
    settings = conn.execute("SELECT * FROM email_settings WHERE id=1").fetchone()
    logs = conn.execute("SELECT * FROM email_logs ORDER BY id DESC LIMIT 20").fetchall()
    conn.close()
    return render_template("email_settings.html", settings=dict(settings) if settings else {}, logs=logs, msg="",
        today_month=date.today().month, today_year=date.today().year, enumerate=enumerate)


@app.route("/email-settings/whatsapp", methods=["POST"])
@amgr
def save_whatsapp_settings():
    conn = get_db()
    conn.execute("""UPDATE email_settings SET
        whatsapp_enabled=?,whatsapp_api_url=?,whatsapp_api_key=? WHERE id=1""",
        (1 if request.form.get("whatsapp_enabled")=="1" else 0,
         request.form.get("whatsapp_api_url",""),
         request.form.get("whatsapp_api_key","")))
    conn.commit(); conn.close()
    return jsonify({"success":True})

@app.route("/email-settings/whatsapp-test", methods=["POST"])
@amgr
def test_whatsapp():
    d = request.json
    phone = d.get("phone","")
    ok, msg = send_whatsapp(phone, "🎉 Test message from Vijayshri Packaging Ltd. PayRoll System!")
    return jsonify({"success":ok,"message":msg})

@app.route("/email-settings/test", methods=["POST"])
@amgr
def test_email():
    d = request.json
    to = d.get("to", get_email_settings().get("email",""))
    ok, msg = send_email(to, "Test Email — Vijayshri PayRoll System",
        f"""{make_email_header()}
        <div style="padding:24px;font-family:Arial,sans-serif;background:white;">
            <h3 style="color:#0052cc;">✅ Email Configuration Successful!</h3>
            <p>Your email settings are working correctly.</p>
            <p style="color:#64748b;font-size:12px;">Sent from Vijayshri Packaging Ltd. PayRoll System</p>
        </div>{make_email_footer()}""", "test")
    return jsonify({"success":ok, "message":msg})

@app.route("/email/send-payslip", methods=["POST"])
@amgr
def send_payslip_email():
    d = request.json
    month = int(d.get("month", date.today().month))
    year  = int(d.get("year",  date.today().year))
    emp_codes = d.get("emp_codes", [])  # empty = all
    conn = get_db()
    if emp_codes:
        recs = conn.execute("""SELECT s.*,e.emp_name,e.email,e.department,e.designation,e.category,
            e.bank_account,e.bank_name FROM salary_records s JOIN employees e ON s.emp_code=e.emp_code
            WHERE s.month=? AND s.year=? AND s.emp_code IN ({})""".format(",".join("?"*len(emp_codes))),
            [month,year]+emp_codes).fetchall()
    else:
        recs = conn.execute("""SELECT s.*,e.emp_name,e.email,e.department,e.designation,e.category,
            e.bank_account,e.bank_name FROM salary_records s JOIN employees e ON s.emp_code=e.emp_code
            WHERE s.month=? AND s.year=?""",(month,year)).fetchall()
    conn.close()
    sent=0; failed=0; no_email=0
    mn = MONTHS[month-1]
    for r in recs:
        if not r["email"]:
            no_email+=1; continue
        html = f"""{make_email_header()}
        <div style="padding:24px;font-family:Arial,sans-serif;background:white;">
            <p>Dear <strong>{r["emp_name"]}</strong>,</p>
            <p>Please find your salary slip for <strong>{mn} {year}</strong> below.</p>
            <table style="width:100%;border-collapse:collapse;margin:16px 0;">
                <tr style="background:#ebf4ff;"><td style="padding:8px 12px;border:1px solid #d0dcf0;font-weight:700;">Component</td><td style="padding:8px 12px;border:1px solid #d0dcf0;font-weight:700;text-align:right;">Amount</td></tr>
                <tr><td style="padding:8px 12px;border:1px solid #d0dcf0;">Basic Salary</td><td style="padding:8px 12px;border:1px solid #d0dcf0;text-align:right;">Rs. {r["basic_earned"]:,.2f}</td></tr>
                <tr style="background:#f8fafc;"><td style="padding:8px 12px;border:1px solid #d0dcf0;">HRA</td><td style="padding:8px 12px;border:1px solid #d0dcf0;text-align:right;">Rs. {r["hra_earned"]:,.2f}</td></tr>
                <tr><td style="padding:8px 12px;border:1px solid #d0dcf0;">Special Allowance</td><td style="padding:8px 12px;border:1px solid #d0dcf0;text-align:right;">Rs. {r["special_earned"]:,.2f}</td></tr>
                {"<tr style='background:#f8fafc;'><td style='padding:8px 12px;border:1px solid #d0dcf0;'>OT Amount</td><td style='padding:8px 12px;border:1px solid #d0dcf0;text-align:right;'>Rs. "+str(f"{r['ot_amount']:,.2f}")+"</td></tr>" if r["category"] == "Associate" else ""}
                <tr style="background:#ebf4ff;font-weight:700;"><td style="padding:8px 12px;border:1px solid #d0dcf0;">Gross Salary</td><td style="padding:8px 12px;border:1px solid #d0dcf0;text-align:right;">Rs. {r["gross"]:,.2f}</td></tr>
                <tr style="color:#dc2626;"><td style="padding:8px 12px;border:1px solid #d0dcf0;">PF Deduction</td><td style="padding:8px 12px;border:1px solid #d0dcf0;text-align:right;">- Rs. {r["pf"]:,.2f}</td></tr>
                <tr style="background:#f8fafc;color:#dc2626;"><td style="padding:8px 12px;border:1px solid #d0dcf0;">ESI Deduction</td><td style="padding:8px 12px;border:1px solid #d0dcf0;text-align:right;">- Rs. {r["esi"]:,.2f}</td></tr>
                <tr style="color:#dc2626;"><td style="padding:8px 12px;border:1px solid #d0dcf0;">TDS</td><td style="padding:8px 12px;border:1px solid #d0dcf0;text-align:right;">- Rs. {r["tds"]:,.2f}</td></tr>
                <tr style="background:#0052cc;color:white;font-weight:800;font-size:15px;"><td style="padding:10px 12px;border:1px solid #0096dc;">NET SALARY</td><td style="padding:10px 12px;border:1px solid #0096dc;text-align:right;">Rs. {r["net_salary"]:,.2f}</td></tr>
            </table>
            <p style="font-size:12px;color:#64748b;">Days Present: {r["present_days"]} / {r["working_days"]} | Bank: {r["bank_account"] or "N/A"} ({r["bank_name"] or ""})</p>
        </div>{make_email_footer()}"""
        ok,_ = send_email(r["email"], f"Salary Slip — {mn} {year} | Vijayshri Packaging Ltd.", html, "payslip")
        if ok: sent+=1
        else: failed+=1
    return jsonify({"success":True,"sent":sent,"failed":failed,"no_email":no_email})

@app.route("/email/send-birthday-wishes", methods=["POST"])
@amgr
def send_birthday_wishes_bulk():
    """Bulk birthday wishes to all employees with birthday today"""
    conn = get_db()
    today = date.today()
    emps = conn.execute("""SELECT * FROM employees WHERE status='Active'
        AND strftime('%m-%d', date_of_birth) = ?""",
        (today.strftime("%m-%d"),)).fetchall()
    conn.close()
    sent=0; no_email=0
    for e in emps:
        if not e["email"]: no_email+=1; continue
        html = f"""{make_email_header()}<div style="padding:32px;font-family:Arial;background:white;text-align:center;">
            <div style="font-size:48px;">🎂</div>
            <h2 style="color:#0052cc;">Happy Birthday, {e["emp_name"]}!</h2>
            <p>Wishing you a wonderful birthday filled with joy!</p>
            <p style="color:#64748b;font-size:12px;">— Vijayshri Packaging Ltd. Family</p>
        </div>{make_email_footer()}"""
        ok,_ = send_email(e["email"], f"Happy Birthday {e['emp_name']}! 🎂", html, "birthday")
        if ok: sent+=1
        else: no_email+=1
    return jsonify({"success":True,"sent":sent,"no_email":no_email,"total":len(emps)})

@app.route("/email/send-anniversary", methods=["POST"])
@amgr
def send_anniversary():
    conn = get_db()
    today = date.today()
    emps = conn.execute("""SELECT * FROM employees WHERE status='Active'
        AND strftime('%m-%d', date_of_joining) = ? AND date_of_joining != ?""",
        (today.strftime("%m-%d"), today.strftime("%Y-%m-%d"))).fetchall()
    conn.close()
    sent=0; no_email=0
    for e in emps:
        if not e["email"]: no_email+=1; continue
        try:
            doj = datetime.strptime(e["date_of_joining"],"%Y-%m-%d").date()
            years = today.year - doj.year
        except: years = 0
        html = f"""{make_email_header()}
        <div style="padding:32px;font-family:Arial,sans-serif;background:white;text-align:center;">
            <div style="font-size:48px;margin-bottom:16px;">🎊</div>
            <h2 style="color:#10b981;font-size:24px;">Happy Work Anniversary, {e["emp_name"]}!</h2>
            <div style="background:#f0fdf4;border-radius:12px;padding:20px;margin:20px 0;">
                <p style="font-size:32px;font-weight:800;color:#10b981;margin:0;">{years} Year{"s" if years>1 else ""}</p>
                <p style="color:#64748b;margin:4px 0;">of Valuable Service</p>
            </div>
            <p style="font-size:14px;color:#1a202c;">Thank you for your dedication and hard work. Your contribution to Vijayshri Packaging is truly valued!</p>
            <p style="font-size:13px;color:#64748b;">Here's to many more years of success together!</p>
        </div>{make_email_footer()}"""
        ok,_ = send_email(e["email"], f"Happy Work Anniversary {e['emp_name']}! 🎊 {years} Year(s)", html, "anniversary")
        if ok: sent+=1
        else: no_email+=1
    return jsonify({"success":True,"sent":sent,"no_email":no_email,"total":len(emps)})

@app.route("/email/send-monthly-summary", methods=["POST"])
@amgr
def send_monthly_summary():
    d = request.json
    month = int(d.get("month", date.today().month))
    year  = int(d.get("year",  date.today().year))
    to_email = d.get("to_email","")
    if not to_email: return jsonify({"success":False,"error":"Enter management email"})
    conn = get_db()
    recs = conn.execute("""SELECT e.category, e.department,
        COUNT(*) as emp_count, SUM(s.gross) as total_gross,
        SUM(s.net_salary) as total_net, SUM(s.pf) as total_pf, SUM(s.esi) as total_esi
        FROM salary_records s JOIN employees e ON s.emp_code=e.emp_code
        WHERE s.month=? AND s.year=? GROUP BY e.department ORDER BY e.department""",(month,year)).fetchall()
    totals = conn.execute("""SELECT COUNT(*) as tc, SUM(net_salary) as tn,
        SUM(gross) as tg, SUM(pf) as tp, SUM(esi) as te
        FROM salary_records WHERE month=? AND year=?""",(month,year)).fetchone()
    conn.close()
    mn = MONTHS[month-1]
    rows_html = "".join([f"""
        <tr style="{'background:#f8fafc;' if i%2 else ''}">
            <td style="padding:8px 12px;border:1px solid #d0dcf0;">{r["department"] or "N/A"}</td>
            <td style="padding:8px 12px;border:1px solid #d0dcf0;text-align:center;">{r["emp_count"]}</td>
            <td style="padding:8px 12px;border:1px solid #d0dcf0;text-align:right;">Rs. {(r["total_gross"] or 0):,.0f}</td>
            <td style="padding:8px 12px;border:1px solid #d0dcf0;text-align:right;color:#dc2626;">Rs. {((r["total_pf"] or 0)+(r["total_esi"] or 0)):,.0f}</td>
            <td style="padding:8px 12px;border:1px solid #d0dcf0;text-align:right;font-weight:700;color:#0052cc;">Rs. {(r["total_net"] or 0):,.0f}</td>
        </tr>""" for i,r in enumerate(recs)])
    html = f"""{make_email_header()}
    <div style="padding:24px;font-family:Arial,sans-serif;background:white;">
        <h3 style="color:#0052cc;">Monthly Salary Summary — {mn} {year}</h3>
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin:16px 0;">
            <div style="background:#ebf4ff;border-radius:8px;padding:14px;text-align:center;">
                <div style="font-size:11px;color:#64748b;text-transform:uppercase;">Total Employees</div>
                <div style="font-size:24px;font-weight:800;color:#0052cc;">{totals["tc"] or 0}</div>
            </div>
            <div style="background:#f0fdf4;border-radius:8px;padding:14px;text-align:center;">
                <div style="font-size:11px;color:#64748b;text-transform:uppercase;">Total Net Payout</div>
                <div style="font-size:20px;font-weight:800;color:#10b981;">Rs. {(totals["tn"] or 0):,.0f}</div>
            </div>
            <div style="background:#fef3c7;border-radius:8px;padding:14px;text-align:center;">
                <div style="font-size:11px;color:#64748b;text-transform:uppercase;">PF + ESI</div>
                <div style="font-size:20px;font-weight:800;color:#f59e0b;">Rs. {((totals["tp"] or 0)+(totals["te"] or 0)):,.0f}</div>
            </div>
        </div>
        <table style="width:100%;border-collapse:collapse;margin:16px 0;">
            <thead>
                <tr style="background:#0052cc;color:white;">
                    <th style="padding:10px 12px;border:1px solid #0096dc;text-align:left;">Department</th>
                    <th style="padding:10px 12px;border:1px solid #0096dc;">Employees</th>
                    <th style="padding:10px 12px;border:1px solid #0096dc;">Gross</th>
                    <th style="padding:10px 12px;border:1px solid #0096dc;">Deductions</th>
                    <th style="padding:10px 12px;border:1px solid #0096dc;">Net Salary</th>
                </tr>
            </thead>
            <tbody>{rows_html}</tbody>
            <tfoot>
                <tr style="background:#0052cc;color:white;font-weight:700;">
                    <td style="padding:10px 12px;border:1px solid #0096dc;">TOTAL</td>
                    <td style="padding:10px 12px;border:1px solid #0096dc;text-align:center;">{totals["tc"] or 0}</td>
                    <td style="padding:10px 12px;border:1px solid #0096dc;text-align:right;">Rs. {(totals["tg"] or 0):,.0f}</td>
                    <td style="padding:10px 12px;border:1px solid #0096dc;text-align:right;">Rs. {((totals["tp"] or 0)+(totals["te"] or 0)):,.0f}</td>
                    <td style="padding:10px 12px;border:1px solid #0096dc;text-align:right;">Rs. {(totals["tn"] or 0):,.0f}</td>
                </tr>
            </tfoot>
        </table>
        <p style="font-size:11px;color:#94a3b8;">Generated by Vijayshri Packaging Ltd. PayRoll System on {datetime.now().strftime("%d %B %Y %I:%M %p")}</p>
    </div>{make_email_footer()}"""
    ok, msg = send_email(to_email, f"Salary Summary — {mn} {year} | Vijayshri Packaging", html, "summary")
    return jsonify({"success":ok,"message":msg})

@app.route("/email/notify-leave", methods=["POST"])
@amgr
def notify_leave_email():
    d = request.json
    leave_id = d.get("leave_id")
    action = d.get("action","approved")  # approved or rejected
    conn = get_db()
    leave = conn.execute("SELECT * FROM leave_requests WHERE id=?", (leave_id,)).fetchone()
    if not leave: conn.close(); return jsonify({"success":False,"error":"Not found"})
    emp = conn.execute("SELECT email,emp_name FROM employees WHERE emp_code=?", (leave["emp_code"],)).fetchone()
    conn.close()
    if not emp or not emp["email"]:
        return jsonify({"success":False,"error":"Employee email not found"})
    color = "#10b981" if action=="approved" else "#dc2626"
    icon  = "✅" if action=="approved" else "❌"
    html  = f"""{make_email_header()}
    <div style="padding:24px;font-family:Arial,sans-serif;background:white;">
        <p>Dear <strong>{emp["emp_name"]}</strong>,</p>
        <div style="background:{'#f0fdf4' if action=='approved' else '#fef2f2'};border:1px solid {color};border-radius:10px;padding:16px;margin:16px 0;">
            <p style="font-size:16px;font-weight:700;color:{color};margin:0 0 8px 0;">{icon} Leave {action.capitalize()}!</p>
            <p style="margin:4px 0;color:#1a202c;"><strong>Type:</strong> {leave["leave_type"]}</p>
            <p style="margin:4px 0;color:#1a202c;"><strong>From:</strong> {leave["from_date"]}</p>
            <p style="margin:4px 0;color:#1a202c;"><strong>To:</strong> {leave["to_date"]}</p>
            <p style="margin:4px 0;color:#1a202c;"><strong>Days:</strong> {leave["days"]}</p>
        </div>
        {"<p style='color:#64748b;'>Your leave request has been approved. Enjoy your time off!</p>" if action=="approved" else f"<p style='color:#64748b;'>Reason: {leave.get('rejection_reason','N/A')}</p>"}
    </div>{make_email_footer()}"""
    ok, msg = send_email(emp["email"], f"Leave {action.capitalize()} — {leave['leave_type']} | Vijayshri Packaging", html, "leave")
    return jsonify({"success":ok,"message":msg})



# ─── HOLIDAYS & EVENTS ────────────────────────────────

@app.route("/holidays")
@amgr
def holidays():
    year = int(request.args.get("year", date.today().year))
    conn = get_db()
    hols = conn.execute("""SELECT * FROM holidays 
        WHERE strftime('%Y', holiday_date)=? 
        ORDER BY holiday_date""", (str(year),)).fetchall()
    depts = conn.execute("SELECT DISTINCT department FROM employees WHERE status='Active' AND department IS NOT NULL ORDER BY department").fetchall()
    conn.close()
    from collections import defaultdict
    hols_list = [dict(h) for h in hols]
    by_month = defaultdict(list)
    for h in hols_list:
        m = int(h["holiday_date"][5:7])
        by_month[m].append(h)
    return render_template("holidays.html", holidays=hols_list, by_month=dict(by_month),
        year=year, months=MONTHS, today=date.today(),
        departments=[d["department"] for d in depts])


def update_attendance_for_holiday(conn, holiday_date, applies_to="All", is_new=True):
    """When holiday is added/edited: update existing attendance records on that date.
    - No punch → mark as Holiday (H)
    - Has punch → mark as Holiday with status_override so OT is calculated with 30min break
    - Working days NOT affected — holiday is OT-only event in payroll
    """
    # Get all employees affected
    cat_filter = ""
    if applies_to == "Staff":     cat_filter = " AND e.category='Staff'"
    elif applies_to == "Associate": cat_filter = " AND e.category!='Staff'"

    rows = conn.execute(f"""
        SELECT a.emp_code, a.att_date, a.in_time, a.out_time,
               a.working_minutes, a.ot_minutes, e.category
        FROM attendance a
        JOIN employees e ON a.emp_code=e.emp_code
        WHERE a.att_date=?{cat_filter}
    """, (holiday_date,)).fetchall()

    for r in rows:
        ec  = r["emp_code"]
        cat = r["category"]
        in_t  = r["in_time"]  or ""
        out_t = r["out_time"] or ""

        if in_t and out_t:
            # Employee worked on this holiday → HP — recalculate OT with 30min break
            result = calc_att(ec, in_t, out_t, cat, status_override="Holiday")
            conn.execute("""UPDATE attendance SET
                status='Holiday', working_minutes=?, ot_minutes=?,
                late_minutes=0, short_minutes=0
                WHERE emp_code=? AND att_date=?""",
                (result["working_minutes"], result["ot_minutes"], ec, holiday_date))
        elif in_t and not out_t:
            # Single punch on holiday → mark Holiday, no OT
            conn.execute("""UPDATE attendance SET status='Holiday', ot_minutes=0
                WHERE emp_code=? AND att_date=?""", (ec, holiday_date))
        else:
            # No punch → mark Holiday (H), no OT
            conn.execute("""UPDATE attendance SET
                status='Holiday', working_minutes=0, ot_minutes=0,
                late_minutes=0, short_minutes=0, is_half_day=0
                WHERE emp_code=? AND att_date=?""", (ec, holiday_date))

    # Also insert Holiday record for employees with NO attendance record on that date
    emps_with_att = {r["emp_code"] for r in rows}
    all_emps = conn.execute(f"""
        SELECT emp_code FROM employees
        WHERE status='Active'{cat_filter}
    """).fetchall()

    for emp in all_emps:
        ec = emp["emp_code"]
        if ec not in emps_with_att:
            conn.execute("""INSERT OR IGNORE INTO attendance
                (emp_code, att_date, in_time, out_time, working_minutes,
                 status, late_minutes, short_minutes, ot_minutes, is_half_day)
                VALUES (?,?, '', '', 0, 'Holiday', 0, 0, 0, 0)""",
                (ec, holiday_date))

@app.route("/holidays/add", methods=["POST"])
@amgr
def add_holiday():
    d = request.json
    conn = get_db()
    try:
        # applies_to: 'All', 'Staff', 'Associate', or 'Department'
        applies_to    = d.get("applies_to", "All")
        dept_list     = ",".join(d.get("departments", [])) if d.get("departments") else ""
        conn.execute("""INSERT INTO holidays 
            (holiday_date, title, holiday_type, description, is_optional,
             applies_to, is_paid, department_list, added_by, created_on)
            VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (d["holiday_date"], d["title"], d.get("holiday_type","National"),
             d.get("description",""), 1 if d.get("is_optional") else 0,
             applies_to, 1 if d.get("is_paid", True) else 0,
             dept_list, session.get("name","HR"), datetime.now().strftime("%Y-%m-%d")))
        conn.commit()
        # Update attendance records for this holiday date
        update_attendance_for_holiday(conn, d["holiday_date"], applies_to)
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
    finally:
        conn.close()

@app.route("/holidays/edit/<int:hid>", methods=["POST"])
@amgr
def edit_holiday(hid):
    d = request.json
    conn = get_db()
    try:
        applies_to = d.get("applies_to", "All")
        dept_list  = ",".join(d.get("departments", [])) if d.get("departments") else ""
        conn.execute("""UPDATE holidays SET title=?, holiday_type=?, description=?,
            holiday_date=?, is_optional=?, applies_to=?, is_paid=?, department_list=?
            WHERE id=?""",
            (d["title"], d.get("holiday_type","National"),
             d.get("description",""), d["holiday_date"],
             1 if d.get("is_optional") else 0,
             applies_to, 1 if d.get("is_paid", True) else 0,
             dept_list, hid))
        conn.commit()
        # Re-sync attendance for updated holiday date
        update_attendance_for_holiday(conn, d["holiday_date"], applies_to)
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
    finally:
        conn.close()

@app.route("/holidays/delete/<int:hid>", methods=["POST"])
@amgr
def delete_holiday(hid):
    conn = get_db()
    conn.execute("DELETE FROM holidays WHERE id=?", (hid,))
    conn.commit(); conn.close()
    return jsonify({"success": True})

@app.route("/holidays/clear-all", methods=["POST"])
@amgr
def clear_all_holidays():
    conn = get_db()
    conn.execute("DELETE FROM holidays")
    conn.commit(); conn.close()
    return jsonify({"success": True})

@app.route("/holidays/get/<int:hid>")
@amgr
def get_holiday(hid):
    conn = get_db()
    h = conn.execute("SELECT * FROM holidays WHERE id=?", (hid,)).fetchone()
    conn.close()
    return jsonify(dict(h)) if h else jsonify({})

@app.route("/holidays/data")
@lreq
def holidays_data():
    """API for calendar page — returns holidays as JSON"""
    year  = int(request.args.get("year",  date.today().year))
    month = int(request.args.get("month", date.today().month))
    conn  = get_db()
    hols  = conn.execute("""SELECT * FROM holidays
        WHERE strftime('%Y-%m', holiday_date)=?
        ORDER BY holiday_date""",
        (f"{year}-{month:02d}",)).fetchall()
    conn.close()
    return jsonify([dict(h) for h in hols])



# ─── EXPERIENCE & RELIEVING LETTERS ─────────────────



@app.route("/letters/warning/<emp_code>")
@amgr
def warning_letter(emp_code):
    conn = get_db()
    emp = conn.execute("SELECT * FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
    if not emp:
        conn.close()
        return "Employee not found", 404
    letter_code = get_next_letter_code("warning", conn, emp["emp_code"], emp["emp_name"])
    _ls = get_letter_settings_b64(conn)
    has_header       = _ls["has_header"]
    has_footer       = _ls["has_footer"]
    has_seal         = _ls["has_seal"]
    has_signature    = _ls["has_signature"]
    has_director_sign= _ls["has_director_sign"]
    has_hr_sign      = _ls["has_hr_sign"]
    sign_b64     = _ls["sign_b64"]
    seal_b64     = _ls["seal_b64"]
    header_b64   = _ls["header_b64"]
    footer_b64   = _ls["footer_b64"]
    director_b64 = _ls["director_b64"]
    hr_b64       = _ls["hr_b64"]
    conn.commit(); conn.close()
    incident_date = request.args.get("incident_date", "")
    incident      = request.args.get("incident", "")
    action        = request.args.get("action", "")
    custom_lines  = request.args.get("custom_lines", "")
    return render_template("warning_letter.html", emp=dict(emp),
        company=COMPANY, today=date.today().strftime("%d %B %Y"),
        letter_code=letter_code, incident_date=incident_date,
        incident=incident, action=action, custom_lines=custom_lines,
        has_header=has_header, has_footer=has_footer,
        has_seal=has_seal, has_signature=has_signature,
        sign_b64=sign_b64, seal_b64=seal_b64, header_b64=header_b64, footer_b64=footer_b64,
        director_b64=director_b64, hr_b64=hr_b64,
        has_director_sign=has_director_sign, has_hr_sign=has_hr_sign)

@app.route("/letters/project-experience/<emp_code>")
@amgr
def project_experience_letter(emp_code):
    from datetime import date as _dt
    conn = get_db()
    emp = conn.execute("SELECT * FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
    if not emp:
        conn.close()
        return "Employee not found", 404

    role_held  = request.args.get("role", "").strip()
    projects   = request.args.get("projects", "").strip()
    skills     = request.args.get("skills", "").strip()

    projects_list = [p.strip().lstrip("-•▸ ") for p in projects.split("\n") if p.strip()] if projects else []
    skills_list   = [s.strip() for s in skills.split(",") if s.strip()] if skills else []

    today = _dt.today()
    issue_date_display = today.strftime("%d %B %Y")

    # Generate letter code and log to document_log
    letter_code = get_next_letter_code("project_experience", conn, emp["emp_code"], emp["emp_name"])
    _ls = get_letter_settings_b64(conn)
    has_header       = _ls["has_header"]
    has_footer       = _ls["has_footer"]
    has_seal         = _ls["has_seal"]
    has_signature    = _ls["has_signature"]
    has_director_sign= _ls["has_director_sign"]
    has_hr_sign      = _ls["has_hr_sign"]
    sign_b64     = _ls["sign_b64"]
    seal_b64     = _ls["seal_b64"]
    header_b64   = _ls["header_b64"]
    footer_b64   = _ls["footer_b64"]
    director_b64 = _ls["director_b64"]
    hr_b64       = _ls["hr_b64"]
    conn.commit()
    conn.close()

    return render_template("project_experience_letter.html",
        emp=dict(emp),
        role_held=role_held,
        projects_list=projects_list,
        skills_list=skills_list,
        issue_date=issue_date_display,
        issue_date_display=issue_date_display,
        letter_code=letter_code,
        has_header=has_header, has_footer=has_footer,
        has_seal=has_seal, has_signature=has_signature,
        sign_b64=sign_b64, seal_b64=seal_b64, header_b64=header_b64, footer_b64=footer_b64,
        director_b64=director_b64, hr_b64=hr_b64,
        has_director_sign=has_director_sign, has_hr_sign=has_hr_sign)

@app.route("/letters/experience/<emp_code>")
@amgr
def experience_letter(emp_code):
    conn = get_db()
    emp = conn.execute("SELECT * FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
    if not emp: conn.close(); return "Employee not found", 404
    letter_code = get_next_letter_code("experience", conn, emp["emp_code"], emp["emp_name"])
    _ls = get_letter_settings_b64(conn)
    has_header       = _ls["has_header"]
    has_footer       = _ls["has_footer"]
    has_seal         = _ls["has_seal"]
    has_signature    = _ls["has_signature"]
    has_director_sign= _ls["has_director_sign"]
    has_hr_sign      = _ls["has_hr_sign"]
    sign_b64     = _ls["sign_b64"]
    seal_b64     = _ls["seal_b64"]
    header_b64   = _ls["header_b64"]
    footer_b64   = _ls["footer_b64"]
    director_b64 = _ls["director_b64"]
    hr_b64       = _ls["hr_b64"]
    conn.commit(); conn.close()
    custom_lines = request.args.get("custom_lines", "").strip()
    return render_template("experience_letter.html", emp=dict(emp),
        company=COMPANY, today=date.today().strftime("%d %B %Y"),
        months=MONTHS, letter_code=letter_code, custom_lines=custom_lines,
        has_header=has_header, has_footer=has_footer, has_seal=has_seal, has_signature=has_signature,
        sign_b64=sign_b64, seal_b64=seal_b64, header_b64=header_b64, footer_b64=footer_b64,
        director_b64=director_b64, hr_b64=hr_b64,
        has_director_sign=has_director_sign, has_hr_sign=has_hr_sign)

@app.route("/letters/relieving/<emp_code>")
@amgr
def relieving_letter(emp_code):
    conn = get_db()
    emp = conn.execute("SELECT * FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
    if not emp: conn.close(); return "Employee not found", 404
    letter_code = get_next_letter_code("relieving", conn, emp["emp_code"], emp["emp_name"])
    _ls = get_letter_settings_b64(conn)
    has_header       = _ls["has_header"]
    has_footer       = _ls["has_footer"]
    has_seal         = _ls["has_seal"]
    has_signature    = _ls["has_signature"]
    has_director_sign= _ls["has_director_sign"]
    has_hr_sign      = _ls["has_hr_sign"]
    sign_b64     = _ls["sign_b64"]
    seal_b64     = _ls["seal_b64"]
    header_b64   = _ls["header_b64"]
    footer_b64   = _ls["footer_b64"]
    director_b64 = _ls["director_b64"]
    hr_b64       = _ls["hr_b64"]
    conn.commit(); conn.close()
    custom_lines = request.args.get("custom_lines", "").strip()
    return render_template("relieving_letter.html", emp=dict(emp),
        company=COMPANY, today=date.today().strftime("%d %B %Y"),
        months=MONTHS, letter_code=letter_code, custom_lines=custom_lines,
        has_header=has_header, has_footer=has_footer, has_seal=has_seal, has_signature=has_signature,
        sign_b64=sign_b64, seal_b64=seal_b64, header_b64=header_b64, footer_b64=footer_b64,
        director_b64=director_b64, hr_b64=hr_b64,
        has_director_sign=has_director_sign, has_hr_sign=has_hr_sign)


# ─── CUSTOM DEDUCTIONS ───────────────────────────────


@app.route("/deductions/bulk-upload", methods=["POST"])
@amgr
def deductions_bulk_upload():
    """Bulk upload deductions from Excel"""
    if "file" not in request.files:
        return jsonify({"success":False,"error":"No file uploaded"})
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(request.files["file"].read()))
        ws = wb.active
        # Auto detect headers
        hdrs = [str(c.value).strip().lower().replace(" ","_") if c.value else "" for c in ws[1]]
        conn = get_db()
        added=0; errors=[]
        for row in ws.iter_rows(min_row=2, values_only=True):
            if not any(row): continue
            d = dict(zip(hdrs, row))
            ec = str(d.get("emp_code","") or "").strip()
            if not ec: continue
            # Normalize emp_code
            if ec.replace(".","").isdigit(): ec = str(int(float(ec)))
            emp = conn.execute("SELECT emp_name FROM employees WHERE emp_code=?", (ec,)).fetchone()
            if not emp: errors.append(f"{ec}: not found"); continue
            ded_type = str(d.get("deduction_type","") or d.get("type","Loan")).strip()
            total    = float(d.get("total_amount","") or d.get("amount",0) or 0)
            monthly  = float(d.get("monthly_amount","") or d.get("monthly",0) or total)
            desc     = str(d.get("description","") or d.get("reason","")).strip()
            sm = int(d.get("start_month","") or date.today().month)
            sy = int(d.get("start_year","") or date.today().year)
            conn.execute("""INSERT INTO custom_deductions
                (emp_code,deduction_type,description,total_amount,monthly_amount,
                 start_month,start_year,status,created_on,created_by)
                VALUES (?,?,?,?,?,?,?,'Active',?,?)""",
                (ec,ded_type,desc,total,monthly,sm,sy,
                 datetime.now().strftime("%Y-%m-%d"),session.get("name","HR")))
            added += 1
        conn.commit(); conn.close()
        return jsonify({"success":True,"added":added,"errors":errors[:5]})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})

@app.route("/deductions/template")
@amgr
def deductions_template():
    """Download deduction bulk upload template"""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    wb = openpyxl.Workbook(); ws = wb.active
    ws.title = "Deductions Template"
    hdrs = ["emp_code","deduction_type","total_amount","monthly_amount","description","start_month","start_year"]
    for i,h in enumerate(hdrs,1):
        cell = ws.cell(1,i,h)
        cell.font = Font(bold=True,color="FFFFFF")
        cell.fill = PatternFill("solid",fgColor="0052CC")
        cell.alignment = Alignment(horizontal="center")
        ws.column_dimensions[openpyxl.utils.get_column_letter(i)].width = 16
    # Sample rows
    samples = [
        ("1001","Loan",5000,500,"House loan",date.today().month,date.today().year),
        ("1001","Lunch",0,200,"Monthly lunch deduction",date.today().month,date.today().year),
        ("1002","Uniform",1500,750,"Uniform advance",date.today().month,date.today().year),
        ("1003","Fine / Penalty",500,500,"Late penalty",date.today().month,date.today().year),
    ]
    for row in samples: ws.append(list(row))
    # Types note
    ws.cell(7,1,"Valid types: Loan, Advance, Lunch, Uniform, Fine / Penalty, Other")
    ws.cell(7,1).font = Font(italic=True,color="888888")
    out = io.BytesIO(); wb.save(out); out.seek(0)
    return send_file(out, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True, download_name="Deductions_Template.xlsx")

@app.route("/export/deductions")
@amgr
def export_deductions():
    """Export all deductions to Excel"""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    conn = get_db()
    try:
        rows = conn.execute("""SELECT d.*, e.emp_name, e.department, e.category
            FROM custom_deductions d LEFT JOIN employees e ON d.emp_code=e.emp_code
            ORDER BY d.status, e.emp_name""").fetchall()
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Deductions"
        hdr_fill = PatternFill("solid", fgColor="4C1D95")
        hdr_font = Font(bold=True, color="FFFFFF", size=10)
        thin = Border(*[Side(style="thin") for _ in range(4)])
        ws.merge_cells("A1:L1")
        t = ws["A1"]; t.value = f"Vijayshri Packaging Ltd. — Deductions Report"
        t.font = Font(bold=True, size=13, color="4C1D95"); t.alignment = Alignment(horizontal="center")
        ws.append([])
        hdrs = ["Emp Code","Name","Department","Category","Deduction Type",
                "Total Amount","Monthly EMI","Amount Deducted","Balance",
                "Start Month","Start Year","Status","Description"]
        ws.append(hdrs)
        hr = ws.max_row
        for c, h in enumerate(hdrs, 1):
            cell = ws.cell(hr, c, h); cell.fill = hdr_fill; cell.font = hdr_font
            cell.alignment = Alignment(horizontal="center"); cell.border = thin
        alt = PatternFill("solid", fgColor="F5F3FF")
        for i, r in enumerate(rows):
            bal = (r["total_amount"] or 0) - (r["amount_deducted"] or 0)
            row_data = [r["emp_code"], r["emp_name"] or "", r["department"] or "",
                        r["category"] or "", r["deduction_type"] or "",
                        r["total_amount"] or 0, r["monthly_amount"] or 0,
                        r["amount_deducted"] or 0, round(bal,2),
                        r["start_month"] or "", r["start_year"] or "",
                        r["status"] or "Active", r["description"] or ""]
            ws.append(row_data); dr = ws.max_row
            if i%2==0:
                for c in range(1, len(hdrs)+1): ws.cell(dr, c).fill = alt
            for c in [6,7,8,9]: ws.cell(dr, c).number_format = "#,##0.00"
        for col, w in zip("ABCDEFGHIJKLM", [8,22,14,10,16,12,10,12,10,8,8,10,20]):
            ws.column_dimensions[col].width = w
        from io import BytesIO
        buf = BytesIO(); wb.save(buf); buf.seek(0)
        fname = f"Deductions_{date.today().strftime('%Y%m%d')}.xlsx"
        return send_file(buf, as_attachment=True, download_name=fname,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception as e:
        return f"Export error: {str(e)}", 500
    finally:
        try: conn.close()
        except: pass


@app.route("/deductions")
@amgr
def deductions():
    cat     = request.args.get("cat","")
    section = request.args.get("section","all")  # all, loan, canteen, fine
    conn = get_db()

    # Section filter
    sec_filter = ""
    if section == "loan":
        sec_filter = " AND d.deduction_type IN ('Loan','Advance')"
    elif section == "canteen":
        sec_filter = " AND d.deduction_type IN ('Lunch','Uniform')"
    elif section == "fine":
        sec_filter = " AND d.deduction_type IN ('Fine / Penalty','Fine')"

    sql = f"""SELECT d.*, e.emp_name, e.department, e.category
        FROM custom_deductions d JOIN employees e ON d.emp_code=e.emp_code
        WHERE 1=1{sec_filter}"""
    if cat == "Staff":      sql += " AND e.category='Staff'"
    elif cat == "NonStaff": sql += " AND e.category!='Staff'"
    sql += " ORDER BY d.status ASC, e.emp_name ASC"
    deds = conn.execute(sql).fetchall()
    total_active  = conn.execute("SELECT COUNT(*) as c FROM custom_deductions WHERE status='Active'").fetchone()["c"]
    total_pending = conn.execute("SELECT COALESCE(SUM(total_amount-amount_deducted),0) as s FROM custom_deductions WHERE status='Active'").fetchone()["s"]
    conn.close()
    return render_template("deductions.html", deductions=deds, cat_filter=cat,
        section=section,
        total_active=total_active, total_pending=total_pending,
        today_month=date.today().month, today_year=date.today().year, enumerate=enumerate,
        months=MONTHS)

@app.route("/deductions/add", methods=["POST"])
@amgr
def add_deduction():
    d = request.json; conn = get_db()
    try:
        emp = conn.execute("SELECT emp_name FROM employees WHERE emp_code=?", (d["emp_code"],)).fetchone()
        if not emp: return jsonify({"success":False,"error":"Employee not found"})
        mode = d.get("deduction_mode","loan")  # 'loan' or 'monthly'
        total  = float(d.get("total_amount",0) or 0)
        monthly = float(d.get("monthly_amount",0) or 0)
        # For monthly mode: total = monthly (single month deduction)
        if mode == "monthly":
            total   = monthly
        sm = int(d.get("start_month") or date.today().month)
        sy = int(d.get("start_year") or date.today().year)
        em = int(d["end_month"]) if d.get("end_month") else None
        ey = int(d["end_year"])  if d.get("end_year")  else None
        conn.execute("""INSERT INTO custom_deductions
            (emp_code,deduction_type,description,total_amount,monthly_amount,
             start_month,start_year,end_month,end_year,deduction_mode,status,created_on,created_by)
            VALUES (?,?,?,?,?,?,?,?,?,?,'Active',?,?)""",
            (d["emp_code"],d["deduction_type"],d.get("description",""),
             total, monthly, sm, sy, em, ey, mode,
             datetime.now().strftime("%Y-%m-%d"),session.get("name","HR")))
        conn.commit(); return jsonify({"success":True})
    except Exception as e: return jsonify({"success":False,"error":str(e)})
    finally: conn.close()

@app.route("/deductions/edit/<int:did>", methods=["POST"])
@amgr
def edit_deduction(did):
    d = request.json; conn = get_db()
    try:
        conn.execute("""UPDATE custom_deductions SET deduction_type=?,description=?,
            total_amount=?,monthly_amount=?,status=? WHERE id=?""",
            (d["deduction_type"],d.get("description",""),
             float(d.get("total_amount",0)),float(d.get("monthly_amount",0)),
             d.get("status","Active"),did))
        conn.commit(); return jsonify({"success":True})
    except Exception as e: return jsonify({"success":False,"error":str(e)})
    finally: conn.close()

@app.route("/deductions/delete/<int:did>", methods=["POST"])
@amgr
def delete_deduction(did):
    conn = get_db()
    conn.execute("DELETE FROM custom_deductions WHERE id=?",(did,))
    conn.commit(); conn.close(); return jsonify({"success":True})

@app.route("/deductions/get/<int:did>")
@amgr
def get_deduction(did):
    conn = get_db()
    d = conn.execute("SELECT * FROM custom_deductions WHERE id=?",(did,)).fetchone()
    conn.close(); return jsonify(dict(d)) if d else jsonify({})

# ─── PUNCH ALERTS ─────────────────────────────────────

def generate_punch_alerts():
    """
    Punch Alert Logic — VPL Vijayshri Packaging
    ═══════════════════════════════════════════════════════

    STAFF:
      Missing OUT → IN hai + no OUT + next day koi bhi IN hai (1-2 day gap allowed)
      Missing IN  → no IN + OUT hai + shift_start + 5 hours gone (lunch tak wait)
                    Night shift valid OUT exception: prev day IN tha + today early punch

    NON-STAFF:
      Missing OUT → IN hai + no OUT + next day koi bhi IN hai (any punch = valid proof)
                    Super OT exception: next day punch < 07:00 = valid OUT, no alert
      Missing IN  → no IN + OUT hai + assigned shift_start + 5 hours gone
                    Night shift valid OUT exception same as Staff

    Common Skip:
      WO, Holiday, Absent, Leave → skip
      Super OT next-day early punch (< 07:00) → skip (valid cross-midnight OUT)
    """
    conn = get_db()
    count = 0
    now_time = datetime.now()  # current time for 5-hour rule check

    emps = conn.execute(
        "SELECT emp_code,emp_name,category FROM employees WHERE status='Active'").fetchall()

    for emp in emps:
      try:
        ec  = emp["emp_code"]
        en  = emp["emp_name"]
        cat = emp["category"]  # 'Staff' or 'Associate'

        # Last 30 days attendance — alerts stay until manually resolved
        records = conn.execute("""
            SELECT * FROM attendance
            WHERE emp_code=?
            AND att_date >= date('now','-30 days')
            ORDER BY att_date ASC""", (ec,)).fetchall()

        if not records:
            continue

        # Shift info — for start_time and is_night detection
        try:
            shift, _ = get_shift_for_emp(ec)
        except:
            shift = None

        shift_name      = shift["shift_name"]  if shift else ""
        shift_start_str = shift["start_time"]  if shift else None
        is_night_shift  = shift and int(shift.get("is_night_shift", 0)) == 1

        # shift_start in minutes (for 5-hour rule)
        shift_start_min = t2m(shift_start_str) if shift_start_str else None

        def _tm(s):
            """time string → minutes"""
            try:
                h, m2 = map(int, str(s).split(":")); return h * 60 + m2
            except:
                return -1

        for i, row in enumerate(records):
            att_date = row["att_date"]
            in_t     = row["in_time"]  or ""
            out_t    = row["out_time"] or ""
            status   = row["status"]   or ""

            # ── Skip non-working / already handled days ────────────
            # Note: "Miss Punch" is NOT skipped — it IS the alert condition itself.
            if status in ["WO", "Holiday", "Absent", "Leave"]:
                continue

            # ══════════════════════════════════════════════════════
            # CASE 1: MISSING PUNCH OUT
            # IN hai, OUT nahi, next day koi bhi IN punch hai
            # ══════════════════════════════════════════════════════
            if in_t and not out_t:

                # Check next record for any IN punch
                next_day_has_any_in = False
                next_day_is_super_ot_out = False  # punch < 07:00 = cross-midnight OUT

                if i + 1 < len(records):
                    next_rec = records[i + 1]
                    try:
                        d1 = datetime.strptime(att_date,               "%Y-%m-%d").date()
                        d2 = datetime.strptime(next_rec["att_date"],   "%Y-%m-%d").date()
                        gap = (d2 - d1).days

                        # Allow 1-2 day gap (weekend/holiday in between)
                        if gap <= 2:
                            next_in = next_rec["in_time"] or ""

                            # Super OT / Night shift valid cross-midnight OUT
                            # If next day's FIRST punch is before 07:00 → it's an OUT punch
                            # that got merged as next-day IN — this is valid, no alert
                            if next_in and _tm(next_in) < 7 * 60:
                                next_day_is_super_ot_out = True
                            elif next_in:
                                next_day_has_any_in = True
                    except:
                        pass

                # Alert only if: next day has real IN (not a cross-midnight OUT)
                if next_day_has_any_in and not next_day_is_super_ot_out:
                    conn.execute("""INSERT OR IGNORE INTO punch_alerts
                        (emp_code,emp_name,alert_date,alert_type,shift_name,details,status)
                        VALUES (?,?,?,'Missing Punch Out',?,?,'Pending')""",
                        (ec, en, att_date, shift_name,
                         f"Punch IN at {in_t} but no Punch OUT. Next day attendance found."))
                    count += 1

            # ══════════════════════════════════════════════════════
            # CASE 2: MISSING PUNCH IN
            # OUT hai, IN nahi
            # Wait karo: shift_start + 5 hours (lunch tak)
            # Night shift exception: prev day IN tha = valid cross-midnight OUT
            # ══════════════════════════════════════════════════════
            elif not in_t and out_t:

                # ── Night shift / cross-midnight valid OUT check ──
                is_valid_cross_midnight = False
                if i > 0:
                    prev_rec = records[i - 1]
                    try:
                        d1 = datetime.strptime(prev_rec["att_date"], "%Y-%m-%d").date()
                        d2 = datetime.strptime(att_date,             "%Y-%m-%d").date()
                        # Consecutive day + prev had IN but no OUT = cross-midnight
                        if (d2 - d1).days == 1 and (prev_rec["in_time"] or "") and not (prev_rec["out_time"] or ""):
                            is_valid_cross_midnight = True
                    except:
                        pass

                if is_valid_cross_midnight:
                    continue  # Valid night/super-OT OUT — no alert

                # ── 5-Hour Rule ───────────────────────────────────
                # Shift start + 5 hours gone hona chahiye tab alert banega
                # Iska matlab: lunch tak wait karo (roughly 13:00-14:00)
                should_alert_now = False

                if shift_start_min is not None:
                    # Today ka current time in minutes (only matters for today's record)
                    try:
                        att_dt = datetime.strptime(att_date, "%Y-%m-%d").date()
                        today  = date.today()

                        if att_dt < today:
                            # Past date → definitely 5 hours gone, alert karo
                            should_alert_now = True
                        elif att_dt == today:
                            # Today ka record → check if 5 hours passed since shift start
                            current_min = now_time.hour * 60 + now_time.minute
                            if current_min >= shift_start_min + (5 * 60):
                                should_alert_now = True
                    except:
                        should_alert_now = True  # safe fallback
                else:
                    # No shift assigned → use default 13:00 as cutoff
                    try:
                        att_dt = datetime.strptime(att_date, "%Y-%m-%d").date()
                        if att_dt < date.today():
                            should_alert_now = True
                        elif att_dt == date.today() and now_time.hour >= 13:
                            should_alert_now = True
                    except:
                        should_alert_now = True

                if should_alert_now:
                    conn.execute("""INSERT OR IGNORE INTO punch_alerts
                        (emp_code,emp_name,alert_date,alert_type,shift_name,details,status)
                        VALUES (?,?,?,'Missing Punch In',?,?,'Pending')""",
                        (ec, en, att_date, shift_name,
                         f"Punch OUT at {out_t} but no Punch IN recorded. (Shift: {shift_name or 'N/A'})"))
                    count += 1

            # ══════════════════════════════════════════════════════
            # CASE 3: NO PUNCH AT ALL — but shift was assigned
            # Status = Absent + shift assigned + shift end passed
            # HR verify kare: genuinely absent tha ya punch miss hua
            # ══════════════════════════════════════════════════════
            elif not in_t and not out_t and status == "Absent":
                # Only alert if shift was assigned for this date
                _has_roster = False
                _shift_end_passed = False
                try:
                    _ns_row = conn.execute("""SELECT s.end_time, s.is_night_shift, s.start_time
                        FROM shifts s
                        JOIN shift_roster_dates srd ON s.id=srd.shift_id
                        WHERE srd.emp_code=? AND srd.shift_date=?""",
                        (ec, att_date)).fetchone()
                    if _ns_row:
                        _has_roster = True
                        # Check if shift end time has passed (past dates always count)
                        att_dt3 = datetime.strptime(att_date, "%Y-%m-%d").date()
                        if att_dt3 < date.today():
                            _shift_end_passed = True
                        elif att_dt3 == date.today():
                            _end_str = _ns_row["end_time"] or "16:30"
                            _eh3, _em3 = map(int, _end_str.split(":")[:2])
                            _end_m3 = _eh3*60+_em3
                            if _ns_row["is_night_shift"] and _end_m3 < 12*60:
                                _end_m3 += 24*60
                            _now_m3 = now_time.hour*60+now_time.minute
                            if _now_m3 > _end_m3 + 30:
                                _shift_end_passed = True
                except: pass

                if _has_roster and _shift_end_passed:
                    conn.execute("""INSERT OR IGNORE INTO punch_alerts
                        (emp_code,emp_name,alert_date,alert_type,shift_name,details,status)
                        VALUES (?,?,?,'No Punch — Verify Attendance',?,?,'Pending')""",
                        (ec, en, att_date, shift_name,
                         f"No punch recorded for assigned shift. Marked Absent — please verify if employee was present."))
                    count += 1

      except Exception as _e:
        print(f"[PUNCH ALERT ERR] {ec} / {att_date if 'att_date' in dir() else '?'}: {_e}")
        continue

    conn.commit()
    conn.close()
    return count

@app.route("/punch-alerts")
@amgr
def punch_alerts():
    generate_punch_alerts()
    conn = get_db()
    from datetime import datetime as _dtnow_pa
    now_min_pa = _dtnow_pa.now().hour * 60 + _dtnow_pa.now().minute
    today_str  = date.today().strftime("%Y-%m-%d")
    GRACE_MIN  = 45  # 45 minutes grace after shift start

    # Get employees with approved leave (they should NOT appear in alerts)
    approved_leave_emps = set()
    leave_rows = conn.execute("""SELECT emp_code FROM leave_requests
        WHERE status='Approved' AND from_date <= ? AND to_date >= ?""",
        (today_str, today_str)).fetchall()
    for lr in leave_rows:
        approved_leave_emps.add(lr["emp_code"])

    raw_alerts = conn.execute("""SELECT pa.*,
        COALESCE(e.emp_name, pa.emp_name, pa.emp_code) as emp_name,
        COALESCE(e.department,'—') as department,
        COALESCE(e.category,'—') as category
        FROM punch_alerts pa
        LEFT JOIN employees e ON pa.emp_code=e.emp_code
        ORDER BY pa.status ASC, pa.alert_date DESC""").fetchall()

    alerts_list = []
    for a in raw_alerts:
        row = dict(a)
        ec  = row["emp_code"]
        alert_date = row["alert_date"]

        # Skip if employee has approved leave for this date
        if ec in approved_leave_emps:
            # Check leave specifically for alert_date
            leave_check = conn.execute("""SELECT id FROM leave_requests
                WHERE emp_code=? AND status='Approved'
                AND from_date <= ? AND to_date >= ?""",
                (ec, alert_date, alert_date)).fetchone()
            if leave_check:
                continue  # Skip — leave approved, not a real miss punch

        # Get shift timing for this employee on this date
        shift_row = conn.execute("""SELECT s.shift_name, s.start_time, s.end_time,
            s.is_night_shift FROM shifts s
            JOIN shift_roster_dates srd ON s.id=srd.shift_id
            WHERE srd.emp_code=? AND srd.shift_date=?""",
            (ec, alert_date)).fetchone()

        if not shift_row:
            # Try fixed assignment
            shift_row = conn.execute("""SELECT s.shift_name, s.start_time, s.end_time,
                s.is_night_shift FROM shifts s
                JOIN employee_shifts es ON s.id=es.shift_id
                WHERE es.emp_code=?""", (ec,)).fetchone()

        shift_name = "—"; start_t = "—"; end_t = "—"
        if shift_row:
            shift_name = shift_row["shift_name"] or "—"
            start_t    = shift_row["start_time"]  or "—"
            end_t      = shift_row["end_time"]     or "—"

            # Grace check: for TODAY's alerts only — if shift hasn't started + 45min yet, skip
            if alert_date == today_str and start_t != "—":
                try:
                    sh_h, sh_m = map(int, start_t.split(":")[:2])
                    sh_min = sh_h * 60 + sh_m
                    if now_min_pa < sh_min + GRACE_MIN:
                        continue  # Shift + 45 min grace not passed yet — not an alert
                except: pass

        row["emp_shift"]  = shift_name
        row["start_time"] = start_t
        row["end_time"]   = end_t
        alerts_list.append(row)

    pending   = sum(1 for a in alerts_list if a["status"] == "Pending")
    total     = len(alerts_list)
    pin_miss  = sum(1 for a in alerts_list if a["alert_type"] == "Missing Punch In")
    pout_miss = sum(1 for a in alerts_list if a["alert_type"] == "Missing Punch Out")
    no_punch  = sum(1 for a in alerts_list if a["alert_type"] == "No Punch — Verify Attendance")
    conn.close()
    return render_template("punch_alerts.html", alerts=alerts_list, pending=pending,
        total=total, pin_miss=pin_miss, pout_miss=pout_miss, no_punch=no_punch,
        today=today_str)

@app.route("/punch-alerts/resolve/<int:aid>", methods=["POST"])
@amgr
def resolve_alert(aid):
    conn = get_db()
    conn.execute("""UPDATE punch_alerts SET status='Resolved',
        resolved_by=?,resolved_on=? WHERE id=?""",
        (session.get("name","HR"),datetime.now().strftime("%Y-%m-%d %H:%M"),aid))
    conn.commit(); conn.close(); return jsonify({"success":True})

@app.route("/punch-alerts/resolve-all", methods=["POST"])
@amgr
def resolve_all_alerts():
    conn = get_db()
    conn.execute("""UPDATE punch_alerts SET status='Resolved',
        resolved_by=?,resolved_on=? WHERE status='Pending'""",
        (session.get("name","HR"),datetime.now().strftime("%Y-%m-%d %H:%M")))
    conn.commit(); conn.close(); return jsonify({"success":True})

# ─── SALARY REVISION ─────────────────────────────────

@app.route("/gratuity-bonus")
@amgr
def gratuity_bonus_page():
    conn = get_db()
    gs = conn.execute("SELECT * FROM gratuity_settings WHERE id=1").fetchone()
    bs = conn.execute("SELECT * FROM bonus_settings WHERE id=1").fetchone()
    gr = conn.execute("""SELECT gr.*, e.department, e.designation FROM gratuity_records gr
        LEFT JOIN employees e ON gr.emp_code=e.emp_code ORDER BY gr.calculated_on DESC""").fetchall()
    br = conn.execute("""SELECT br.*, e.department FROM bonus_records br
        LEFT JOIN employees e ON br.emp_code=e.emp_code ORDER BY br.year DESC, br.emp_name""").fetchall()
    conn.close()
    return render_template("gratuity_bonus.html",
        gs=dict(gs) if gs else {}, bs=dict(bs) if bs else {},
        gratuity_records=[dict(r) for r in gr],
        bonus_records=[dict(r) for r in br],
        months=MONTHS, years=list(range(date.today().year-5, date.today().year+2)))


@app.route("/gratuity-bonus/settings", methods=["POST"])
@amgr
def save_gratuity_bonus_settings():
    d = request.json; conn = get_db()
    try:
        section = d.get("section","gratuity")
        if section == "gratuity":
            conn.execute("""UPDATE gratuity_settings SET
                formula=?,min_years=?,rate_per_year=?,days_divisor=?,
                taxable_limit=?,include_hra=?,include_special=?,notes=?,
                updated_on=datetime('now') WHERE id=1""",
                (d.get("formula","last_basic_26"),
                 float(d.get("min_years",5)),float(d.get("rate_per_year",15)),
                 int(d.get("days_divisor",26)),float(d.get("taxable_limit",2000000)),
                 1 if d.get("include_hra") else 0,1 if d.get("include_special") else 0,
                 d.get("notes","")))
        else:
            conn.execute("""UPDATE bonus_settings SET
                formula=?,min_rate_pct=?,max_rate_pct=?,wage_ceiling=?,
                calculation_base=?,bonus_ceiling_wages=?,min_working_days=?,
                applicable_category=?,notes=?,updated_on=datetime('now') WHERE id=1""",
                (d.get("formula","statutory"),float(d.get("min_rate_pct",8.33)),
                 float(d.get("max_rate_pct",20)),float(d.get("wage_ceiling",21000)),
                 d.get("calculation_base","basic_hra"),float(d.get("bonus_ceiling_wages",7000)),
                 int(d.get("min_working_days",30)),d.get("applicable_category","All"),
                 d.get("notes","")))
        conn.commit()
        return jsonify({"success":True})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})
    finally: conn.close()


@app.route("/gratuity-bonus/encash-gratuity", methods=["POST"])
@amgr
def encash_gratuity():
    """Mark gratuity record as paid/encashed"""
    d = request.json; conn = get_db()
    try:
        rec_id   = int(d.get("id", 0))
        remarks  = str(d.get("remarks","Gratuity paid"))
        rec = conn.execute("SELECT * FROM gratuity_records WHERE id=?", (rec_id,)).fetchone()
        if not rec: return jsonify({"success":False,"error":"Record not found"})
        if rec["encashed"]: return jsonify({"success":False,"error":"Already encashed"})
        conn.execute("""UPDATE gratuity_records SET encashed=1, encashed_on=datetime('now'),
            encashed_by=?, remarks=? WHERE id=?""",
            (session.get("name","HR"), remarks, rec_id))
        conn.commit()
        return jsonify({"success":True,
            "message":f"✅ Gratuity of ₹{rec['gratuity_amount']:,.2f} marked as paid for {rec['emp_name']}"})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})
    finally: conn.close()


@app.route("/gratuity-bonus/leave-earn-log")
@amgr
def leave_earn_log():
    """View auto leave earn history"""
    year = int(request.args.get("year", date.today().year))
    cat  = request.args.get("cat","")
    conn = get_db()
    sql = """SELECT l.*, e.emp_name, e.department, e.category
        FROM leave_earn_log l JOIN employees e ON l.emp_code=e.emp_code
        WHERE l.year=?"""
    params = [year]
    if cat: sql += " AND e.category=?"; params.append(cat)
    sql += " ORDER BY l.month DESC, e.emp_name"
    logs = conn.execute(sql, params).fetchall()
    # Summary by month
    summary = conn.execute("""SELECT month,
        COUNT(*) as emp_count, SUM(el_credited) as total_el, SUM(cl_credited) as total_cl
        FROM leave_earn_log WHERE year=? GROUP BY month ORDER BY month""", (year,)).fetchall()
    conn.close()
    return jsonify({"success":True,
        "logs":[dict(r) for r in logs],
        "summary":[dict(r) for r in summary],
        "year":year})


@app.route("/gratuity-bonus/calculate-gratuity", methods=["POST"])
@amgr
def calculate_gratuity():
    d = request.json; conn = get_db()
    try:
        emp_code   = str(d.get("emp_code","")).strip()
        exit_date  = str(d.get("exit_date", date.today().isoformat()))
        remarks    = str(d.get("remarks",""))
        emp = conn.execute("SELECT * FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
        if not emp: return jsonify({"success":False,"error":"Employee not found"})
        gs = conn.execute("SELECT * FROM gratuity_settings WHERE id=1").fetchone()
        gs = dict(gs) if gs else {}
        doj_str = emp["date_of_joining"] or ""
        if not doj_str: return jsonify({"success":False,"error":"Date of joining not set"})
        doj = date.fromisoformat(doj_str[:10])
        dox = date.fromisoformat(exit_date[:10])
        # Years of service (Gratuity Act: partial year >= 6 months counts as full year)
        total_days = (dox - doj).days
        years_raw  = total_days / 365.25
        years_service = int(years_raw)
        months_rem = (years_raw - years_service) * 12
        if months_rem >= 6: years_service += 1  # Round up per Gratuity Act
        min_years = float(gs.get("min_years",5))
        if years_raw < min_years:
            return jsonify({"success":False,"error":f"Minimum {min_years} years required. Employee has {years_raw:.1f} years."})
        basic = float(emp["basic"] or 0)
        hra   = float(emp["hra"] or 0) if gs.get("include_hra") else 0
        spec  = float(emp["special_allowance"] or 0) if gs.get("include_special") else 0
        last_wages = basic + hra + spec
        rate_per_year = float(gs.get("rate_per_year",15))
        divisor = int(gs.get("days_divisor",26))
        # Standard formula: (Last Basic Wages / 26) × 15 × Years of Service
        gratuity = round((last_wages / divisor) * rate_per_year * years_service, 2)
        taxable_limit = float(gs.get("taxable_limit",2000000))
        tax_exempt = min(gratuity, taxable_limit)
        taxable = max(0, gratuity - tax_exempt)
        conn.execute("""INSERT INTO gratuity_records
            (emp_code,emp_name,department,date_of_joining,date_of_exit,years_of_service,
             last_basic,last_hra,gratuity_amount,tax_exempt,taxable,status,calculated_on,remarks)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,'Calculated',datetime('now'),?)""",
            (emp_code,emp["emp_name"],emp["department"],doj_str,exit_date,
             years_raw,basic,hra,gratuity,tax_exempt,taxable,remarks))
        conn.commit()
        return jsonify({"success":True,"gratuity":gratuity,"years":years_raw,
            "years_service":years_service,"last_wages":last_wages,
            "tax_exempt":tax_exempt,"taxable":taxable,
            "formula":f"({last_wages}/{divisor}) × {rate_per_year} × {years_service} years"})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})
    finally: conn.close()


@app.route("/gratuity-bonus/calculate-bonus", methods=["POST"])
@amgr
def calculate_bonus():
    d = request.json; conn = get_db()
    try:
        year = int(d.get("year", date.today().year))
        rate = float(d.get("rate_pct", 8.33))
        dept = d.get("dept",""); cat = d.get("category","")
        bs = conn.execute("SELECT * FROM bonus_settings WHERE id=1").fetchone()
        bs = dict(bs) if bs else {}
        wage_ceil  = float(bs.get("wage_ceiling",21000))
        bonus_ceil = float(bs.get("bonus_ceiling_wages",7000))
        min_days   = int(bs.get("min_working_days",30))
        # Get attendance summary for the year to check working days
        sql = "SELECT e.emp_code,e.emp_name,e.department,e.category,e.basic,e.hra FROM employees e WHERE e.status='Active'"
        params = []
        if dept: sql += " AND e.department=?"; params.append(dept)
        if cat:  sql += " AND e.category=?";  params.append(cat)
        emps = conn.execute(sql, params).fetchall()
        processed = 0; skipped = 0
        for e in emps:
            basic = float(e["basic"] or 0); hra = float(e["hra"] or 0)
            wages = basic + hra
            if wages > wage_ceil:
                skipped += 1; continue  # Above wage ceiling — not eligible
            # Bonus calculated on actual wages or ceiling (whichever is lower)
            bonus_wages = min(wages, bonus_ceil)
            bonus_months = 12  # Full year
            bonus_amount = round(bonus_wages * 12 * rate / 100, 2)
            conn.execute("""INSERT OR REPLACE INTO bonus_records
                (emp_code,emp_name,department,year,months_worked,basic_wages,
                 bonus_rate_pct,bonus_amount,status,calculated_on)
                VALUES (?,?,?,?,?,?,?,?,'Pending',datetime('now'))""",
                (e["emp_code"],e["emp_name"],e["department"],year,
                 bonus_months,bonus_wages,rate,bonus_amount))
            processed += 1
        conn.commit()
        return jsonify({"success":True,"processed":processed,"skipped":skipped,
            "message":f"✅ Bonus calculated for {processed} employees @ {rate}% for {year}. {skipped} skipped (above ₹{wage_ceil:,.0f} wage ceiling)."})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})
    finally: conn.close()


@app.route("/export/gratuity-bonus")
@amgr
def export_gratuity_bonus():
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    report_type = request.args.get("type","bonus")
    year = request.args.get("year", date.today().year)
    conn = get_db()
    wb = openpyxl.Workbook()
    if report_type == "bonus":
        ws = wb.active; ws.title = f"Bonus {year}"
        rows = conn.execute("SELECT * FROM bonus_records WHERE year=? ORDER BY department,emp_name", (year,)).fetchall()
        ws.append(["Code","Name","Department","Year","Months","Basic Wages","Rate %","Bonus Amount","Status"])
        for r in rows:
            ws.append([r["emp_code"],r["emp_name"],r["department"],r["year"],
                       r["months_worked"],r["basic_wages"],r["bonus_rate_pct"],
                       r["bonus_amount"],r["status"]])
        fname = f"Bonus_Report_{year}.xlsx"
    else:
        ws = wb.active; ws.title = "Gratuity Records"
        rows = conn.execute("SELECT * FROM gratuity_records ORDER BY calculated_on DESC").fetchall()
        ws.append(["Code","Name","Department","DOJ","Exit Date","Years","Last Basic","Gratuity","Tax Exempt","Taxable","Status"])
        for r in rows:
            ws.append([r["emp_code"],r["emp_name"],r["department"],r["date_of_joining"],
                       r["date_of_exit"],round(r["years_of_service"],2) if r["years_of_service"] else 0,
                       r["last_basic"],r["gratuity_amount"],r["tax_exempt"],r["taxable"],r["status"]])
        fname = "Gratuity_Records.xlsx"
    conn.close()
    # Style header
    hdr_fill = PatternFill("solid", fgColor="003580")
    for cell in ws[1]: cell.fill=hdr_fill; cell.font=Font(bold=True,color="FFFFFF"); cell.alignment=Alignment(horizontal="center")
    for col in ws.columns:
        ws.column_dimensions[col[0].column_letter].width = max(len(str(col[0].value or "")),12)
    from io import BytesIO
    buf = BytesIO(); wb.save(buf); buf.seek(0)
    return send_file(buf, as_attachment=True, download_name=fname,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/export/salary-revision")
@amgr
def export_salary_revision():
    """Export salary revision history to Excel"""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    conn = get_db()
    try:
        # Use actual table name: increments (joined with employees)
        rows = conn.execute("""SELECT i.*, e.emp_name, e.department, e.category,
            e.designation, e.hra, e.special_allowance
            FROM increments i JOIN employees e ON i.emp_code=e.emp_code
            ORDER BY i.effective_date DESC, e.emp_name""").fetchall()
        wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Salary Revision History"
        hdr_fill = PatternFill("solid", fgColor="003580")
        hdr_font = Font(bold=True, color="FFFFFF", size=10)
        thin = Border(*[Side(style="thin") for _ in range(4)])
        ws.merge_cells("A1:J1")
        t = ws["A1"]; t.value = "Vijayshri Packaging Ltd. — Salary Revision History"
        t.font = Font(bold=True, size=13, color="003580")
        t.alignment = Alignment(horizontal="center")
        ws.row_dimensions[1].height = 20
        ws.append([])
        hdrs = ["Emp Code","Name","Department","Category","Designation",
                "Effective Date","Old Basic","New Basic","Increment Amt","Increment %","Reason"]
        ws.append(hdrs)
        hr = ws.max_row
        for c, h in enumerate(hdrs, 1):
            cell = ws.cell(hr, c, h); cell.fill = hdr_fill; cell.font = hdr_font
            cell.alignment = Alignment(horizontal="center"); cell.border = thin
        alt = PatternFill("solid", fgColor="EBF5FF")
        for i, r in enumerate(rows):
            old_b = float(r["old_basic"] or 0)
            new_b = float(r["new_basic"] or 0)
            inc_amt = round(new_b - old_b, 2)
            inc_pct = round(inc_amt / old_b * 100, 2) if old_b else 0
            row_data = [r["emp_code"], r["emp_name"], r["department"] or "",
                        r["category"] or "", r["designation"] or "",
                        r["effective_date"] or "", old_b, new_b, inc_amt, inc_pct,
                        r["reason"] or ""]
            ws.append(row_data); dr = ws.max_row
            if i % 2 == 0:
                for c in range(1, len(hdrs)+1): ws.cell(dr, c).fill = alt
            for c in [7,8,9]: ws.cell(dr, c).number_format = "#,##0.00"
            ws.cell(dr, 10).number_format = "0.00"
        for col, w in zip("ABCDEFGHIJK", [8,22,14,10,14,12,10,10,10,9,20]):
            ws.column_dimensions[col].width = w
        from io import BytesIO
        buf = BytesIO(); wb.save(buf); buf.seek(0)
        fname = f"Salary_Revision_{date.today().strftime('%Y%m%d')}.xlsx"
        return send_file(buf, as_attachment=True, download_name=fname,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception as e:
        return f"Export error: {str(e)}", 500
    finally:
        try: conn.close()
        except: pass


@app.route("/salary/revision-report")
@amgr
def salary_revision_report():
    from datetime import date as _dt_today
    conn = get_db()
    revisions = conn.execute("""SELECT i.*, e.emp_name, e.department, e.category
        FROM increments i JOIN employees e ON i.emp_code=e.emp_code
        ORDER BY i.done_on DESC""").fetchall()
    conn.close()
    from datetime import date as _dt_rev
    return render_template("salary_revision.html", revisions=revisions, months=MONTHS, date=_dt_rev)

@app.route("/salary/history/<emp_code>")
@amgr
def salary_history(emp_code):
    conn = get_db()
    emp = conn.execute("SELECT * FROM employees WHERE emp_code=?",(emp_code,)).fetchone()
    if not emp: conn.close(); return "Not found",404
    increments = conn.execute("SELECT * FROM increments WHERE emp_code=? ORDER BY effective_date DESC",(emp_code,)).fetchall()
    records    = conn.execute("SELECT * FROM salary_records WHERE emp_code=? ORDER BY year DESC,month DESC LIMIT 24",(emp_code,)).fetchall()
    conn.close()
    return render_template("salary_history.html",emp=dict(emp),increments=increments,records=records,months=MONTHS)

@app.route("/salary/increment-letter/<emp_code>")
@amgr
def increment_letter(emp_code):
    conn = get_db()
    emp      = conn.execute("SELECT * FROM employees WHERE emp_code=?",(emp_code,)).fetchone()
    last_inc = conn.execute("SELECT * FROM increments WHERE emp_code=? ORDER BY effective_date DESC LIMIT 1",(emp_code,)).fetchone()
    if not emp: conn.close(); return "Not found",404
    letter_code = get_next_letter_code("increment", conn, emp["emp_code"], emp["emp_name"])
    _ls = get_letter_settings_b64(conn)
    has_header       = _ls["has_header"]
    has_footer       = _ls["has_footer"]
    has_seal         = _ls["has_seal"]
    has_signature    = _ls["has_signature"]
    has_director_sign= _ls["has_director_sign"]
    has_hr_sign      = _ls["has_hr_sign"]
    sign_b64     = _ls["sign_b64"]
    seal_b64     = _ls["seal_b64"]
    header_b64   = _ls["header_b64"]
    footer_b64   = _ls["footer_b64"]
    director_b64 = _ls["director_b64"]
    hr_b64       = _ls["hr_b64"]
    conn.commit(); conn.close()
    custom_lines = request.args.get("custom_lines", "").strip()
    return render_template("increment_letter.html",emp=dict(emp),
        increment=dict(last_inc) if last_inc else None,
        company=COMPANY,today=date.today().strftime("%d %B %Y"),months=MONTHS,
        letter_code=letter_code, custom_lines=custom_lines,
        has_header=has_header, has_footer=has_footer, has_seal=has_seal, has_signature=has_signature,
        sign_b64=sign_b64, seal_b64=seal_b64, header_b64=header_b64, footer_b64=footer_b64,
        director_b64=director_b64, hr_b64=hr_b64,
        has_director_sign=has_director_sign, has_hr_sign=has_hr_sign)


# ─── MULTIPLE MACHINES MANAGEMENT ───────────────────

@app.route("/attendance/machines")
@amgr
def machines_list():
    from datetime import datetime as _dt_m, timedelta as _td_m
    conn = get_db()
    machines_raw = conn.execute("SELECT * FROM machines ORDER BY machine_name").fetchall()
    # Get ADMS server address from settings
    _adms_addr = conn.execute("SELECT value FROM app_settings WHERE key='adms_server_address'").fetchone()
    _adms_port = conn.execute("SELECT value FROM app_settings WHERE key='adms_server_port'").fetchone()
    adms_server_address = (_adms_addr["value"] if _adms_addr and _adms_addr["value"] else "")
    adms_server_port    = (_adms_port["value"] if _adms_port and _adms_port["value"] else "5000")
    conn.close()
    # Compute adms_online — True if adms_last_seen within last 20 minutes
    machines = []
    for m in machines_raw:
        md = dict(m)
        adms_online = False
        if md.get("connection_mode") == "adms" and md.get("adms_last_seen"):
            try:
                last = _dt_m.strptime(md["adms_last_seen"], "%Y-%m-%d %H:%M")
                adms_online = (_dt_m.now() - last).total_seconds() <= 1200  # 20 min
            except: pass
        md["adms_online"] = adms_online
        machines.append(md)
    return render_template("machines.html", machines=machines,
        adms_server_address=adms_server_address,
        adms_server_port=adms_server_port,
        today_month=date.today().month, today_year=date.today().year,
        today_month_name=MONTHS[date.today().month-1])

@app.route("/machines/save-adms-settings", methods=["POST"])
@amgr
def save_adms_settings():
    d = request.json; conn = get_db()
    try:
        conn.execute("INSERT OR REPLACE INTO app_settings (key,value) VALUES ('adms_server_address',?)",
                     (d.get("adms_server_address",""),))
        conn.execute("INSERT OR REPLACE INTO app_settings (key,value) VALUES ('adms_server_port',?)",
                     (d.get("adms_server_port","5000"),))
        conn.commit()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
    finally: conn.close()


@app.route("/attendance/machines/add", methods=["POST"])
@amgr
def add_machine():
    d = request.json; conn = get_db()
    try:
        conn.execute("""INSERT INTO machines 
            (machine_name,ip_address,port,password,location,serial_number,connection_mode,is_active,created_on)
            VALUES (?,?,?,?,?,?,?,1,date('now'))""",
            (d["machine_name"],d["ip_address"],int(d.get("port",4370)),
             int(d.get("password",0)),d.get("location",""),d.get("serial_number",""),
             d.get("connection_mode","zk")))
        conn.commit()
        return jsonify({"success":True})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})
    finally: conn.close()

@app.route("/machines/get/<int:mid>")
@amgr
def get_machine(mid):
    conn = get_db()
    m = conn.execute("SELECT * FROM machines WHERE id=?", (mid,)).fetchone()
    conn.close()
    return jsonify(dict(m)) if m else jsonify({})

@app.route("/attendance/machines/edit/<int:mid>", methods=["POST"])
@amgr
def edit_machine(mid):
    d = request.json; conn = get_db()
    try:
        conn.execute("""UPDATE machines SET machine_name=?,ip_address=?,port=?,
            password=?,location=?,serial_number=?,connection_mode=?,is_active=? WHERE id=?""",
            (d["machine_name"],d["ip_address"],int(d.get("port",4370)),
             int(d.get("password",0)),d.get("location",""),
             d.get("serial_number",""),d.get("connection_mode","zk"),
             1 if d.get("is_active") else 0, mid))
        conn.commit()
        return jsonify({"success":True})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})
    finally: conn.close()

@app.route("/attendance/machines/delete/<int:mid>", methods=["POST"])
@amgr
def delete_machine(mid):
    conn = get_db()
    conn.execute("DELETE FROM machines WHERE id=?", (mid,))
    conn.commit(); conn.close()
    return jsonify({"success":True})

@app.route("/attendance/import-all-machines", methods=["POST"])
@amgr
def import_all_machines():
    """Import attendance from ALL active machines"""
    data   = request.json
    month  = int(data.get("month", date.today().month))
    year   = int(data.get("year",  date.today().year))
    all_months = data.get("all_months", False)
    conn   = get_db()
    machines = conn.execute("SELECT * FROM machines WHERE is_active=1").fetchall()
    conn.close()
    total_imported = 0; total_logs = 0
    results = []
    for machine in machines:
        try:
            from zk import ZK
            from collections import defaultdict
            from datetime import datetime as _dtt_all
            zk  = ZK(machine["ip_address"], port=machine["port"], timeout=60,
                    password=machine["password"] or 0, force_udp=False, ommit_ping=True)
            czk = zk.connect(); czk.disable_device()
            logs= czk.get_attendance(); czk.enable_device(); czk.disconnect()
            total_logs += len(logs)
            emp_punches_all = defaultdict(list)
            for log in logs:
                if all_months or (log.timestamp.month==month and log.timestamp.year==year):
                    emp_punches_all[str(log.user_id)].append(log.timestamp)
            conn2 = get_db()
            all_shifts_all = get_all_shifts(conn2)
            imported = 0; skipped = 0
            for uid_raw, punches in emp_punches_all.items():
                emp, ec = find_emp_by_machine_id(conn2, uid_raw)
                if not emp: skipped+=1; continue
                category = emp["category"]
                grp_shifts = []
                try:
                    gr = conn2.execute("""SELECT s.* FROM shifts s
                        JOIN shift_group_members sgm ON s.id=sgm.shift_id
                        JOIN employee_shift_groups esg ON sgm.group_id=esg.group_id
                        WHERE esg.emp_code=? AND s.is_active=1""", (ec,)).fetchall()
                    grp_shifts = [dict(r) for r in gr]
                except: pass
                use_sh = grp_shifts if grp_shifts else all_shifts_all
                pairs = etimetrack_pair_punches(sorted(set(punches)), use_sh)
                for pair in pairs:
                    att_date = pair["date"]
                    if not all_months:
                        try:
                            from datetime import date as _dtc_all
                            ad = _dtc_all.fromisoformat(att_date)
                            if ad.month != month or ad.year != year: continue
                        except: pass
                    try:
                        from datetime import date as _dtc2
                        att_d2 = _dtc2.fromisoformat(att_date)
                        auto_status = "WOP" if att_d2.weekday()==get_emp_weekly_off_num(ec, conn) else "Present"
                        if auto_status == "Present":
                            hol = conn2.execute("""SELECT id FROM holidays WHERE holiday_date=?
                                AND (applies_to='All' OR applies_to IS NULL OR applies_to='' OR applies_to=? OR applies_to='Staff' OR applies_to='Associate')""",
                                (att_date, category)).fetchone()
                            if hol: auto_status = "Holiday"
                    except: auto_status = "Present"
                    # Miss Punch: only after shift end + 30 min grace
                    if pair["in_time"] and not pair["out_time"] and auto_status not in ("WOP", "Holiday"):
                        try:
                            from datetime import datetime as _dtnow_am
                            _now_am = _dtnow_am.now().hour * 60 + _dtnow_am.now().minute
                            _sh_am = conn2.execute("""SELECT s.end_time, s.is_night_shift
                                FROM shifts s JOIN shift_roster_dates srd ON s.id=srd.shift_id
                                WHERE srd.emp_code=? AND srd.shift_date=?""",
                                (ec, att_date)).fetchone()
                            if _sh_am and _sh_am["end_time"]:
                                _eh_am, _em_am = map(int, _sh_am["end_time"].split(":")[:2])
                                _end_am = _eh_am*60+_em_am
                                if _sh_am["is_night_shift"] and _end_am < 12*60:
                                    _end_am += 24*60
                                if _now_am > _end_am + 30:
                                    auto_status = "Miss Punch"
                            else:
                                _pi = pair["in_time"] or ""
                                _ih, _im = map(int, _pi.split(":")[:2])
                                if _now_am > _ih*60+_im+9*60:
                                    auto_status = "Miss Punch"
                        except:
                            auto_status = "Miss Punch"
                    save_att_row(conn2, ec, att_date, pair["in_time"], pair["out_time"],
                                category, status=auto_status)
                    imported+=1
            conn2.execute("UPDATE machines SET last_sync=?,last_sync_count=? WHERE id=?",
                         (datetime.now().strftime("%Y-%m-%d %H:%M"),imported,machine["id"]))
            conn2.commit(); conn2.close()
            total_imported += imported
            results.append({"machine":machine["machine_name"],"ip":machine["ip_address"],
                           "status":"success","imported":imported,"total":len(logs)})
        except Exception as e:
            results.append({"machine":machine["machine_name"],"ip":machine["ip_address"],
                           "status":"error","error":str(e)})
    return jsonify({"success":True,"total_imported":total_imported,
                   "total_logs":total_logs,"machines":results})


# ─── SHIFT MANAGEMENT ────────────────────────────────

@app.route("/shifts")
@amgr
def shifts():
    conn = get_db()
    shifts_list = conn.execute("SELECT * FROM shifts ORDER BY category, shift_name").fetchall()
    assignments = conn.execute("""SELECT es.*, e.emp_name, e.department, e.category, s.shift_name
        FROM employee_shifts es
        JOIN employees e ON es.emp_code=e.emp_code
        JOIN shifts s ON es.shift_id=s.id
        ORDER BY e.category, e.emp_name""").fetchall()
    emps = conn.execute("SELECT emp_code,emp_name,department,category FROM employees WHERE status='Active' ORDER BY category,emp_name").fetchall()
    conn.close()
    return render_template("shifts.html", shifts=shifts_list, assignments=assignments, employees=emps)

@app.route("/shifts/add", methods=["POST"])
@amgr
def add_shift():
    d = request.json; conn = get_db()
    try:
        st = d.get("start_time","09:00"); et = d.get("end_time","18:00")
        def t2m_l(t):
            h,m = map(int,t.split(":")); return h*60+m
        start_min = t2m_l(st); end_min = t2m_l(et)
        is_next_day = int(d.get("is_next_day", 1 if end_min < start_min else 0))
        if is_next_day and end_min < start_min: end_min += 24*60
        wh = round(float(d.get("working_hours","0") or 0) or (end_min-start_min)/60, 2)
        is_night = 1 if t2m_l(et) < t2m_l(st) else 0
        pbb  = int(d.get("punch_begin_before") or 60)
        ai_s = d.get("allowed_in_start","") or None
        ai_e = d.get("allowed_in_end","")   or None
        if not ai_s:
            def _am(t,m):
                hh,mm=map(int,t.split(":")); total=(hh*60+mm+m)%1440; return f"{total//60:02d}:{total%60:02d}"
            ai_s = _am(st,-pbb); ai_e = _am(st,180)
        conn.execute("""INSERT INTO shifts
            (shift_name,shift_code,category,start_time,end_time,is_next_day,
             working_hours,grace_minutes,ot_formula,punch_begin_before,
             half_day_minutes,neglect_last_in,
             is_night_shift,allowed_in_start,allowed_in_end,is_active,created_on,
             half_day_min_minutes,full_day_min_minutes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,date('now'),?,?)""",
            (d["shift_name"],d["shift_code"],d.get("category","Associate"),
             st,et,is_next_day,wh,int(d.get("grace_minutes",15)),
             d.get("ot_formula","total_minus_shift"),pbb,
             int(d.get("half_day_minutes",240)),int(d.get("neglect_last_in",0)),
             is_night,ai_s,ai_e,
             int(d.get("half_day_min_minutes",180)),
             int(d.get("full_day_min_minutes",390))))
        conn.commit()
        return jsonify({"success":True})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})
    finally: conn.close()

@app.route("/shifts/edit/<int:sid>", methods=["POST"])
@amgr
def edit_shift(sid):
    d = request.json; conn = get_db()
    try:
        st = d.get("start_time","09:00"); et = d.get("end_time","18:00")
        def t2m_l(t):
            h,m = map(int,t.split(":")); return h*60+m
        start_min = t2m_l(st); end_min = t2m_l(et)
        is_next_day = int(d.get("is_next_day", 1 if end_min < start_min else 0))
        if is_next_day and end_min < start_min: end_min += 24*60
        wh = round(float(d.get("working_hours","0") or 0) or (end_min-start_min)/60, 2)
        is_night = 1 if t2m_l(et) < t2m_l(st) else 0
        pbb  = int(d.get("punch_begin_before") or 60)
        ai_s = d.get("allowed_in_start","") or None
        ai_e = d.get("allowed_in_end","")   or None
        if not ai_s:
            def _am(t,m):
                hh,mm=map(int,t.split(":")); total=(hh*60+mm+m)%1440; return f"{total//60:02d}:{total%60:02d}"
            ai_s = _am(st,-pbb); ai_e = _am(st,180)
        conn.execute("""UPDATE shifts SET
            shift_name=?,shift_code=?,category=?,start_time=?,end_time=?,
            is_next_day=?,working_hours=?,grace_minutes=?,ot_formula=?,
            punch_begin_before=?,half_day_minutes=?,neglect_last_in=?,
            is_night_shift=?,allowed_in_start=?,allowed_in_end=?,is_active=?,
            half_day_min_minutes=?,full_day_min_minutes=?
            WHERE id=?""",
            (d["shift_name"],d["shift_code"],d.get("category","Associate"),st,et,
             is_next_day,wh,int(d.get("grace_minutes",15)),
             d.get("ot_formula","total_minus_shift"),pbb,
             int(d.get("half_day_minutes",240)),int(d.get("neglect_last_in",0)),
             is_night,ai_s,ai_e,
             1 if d.get("is_active",True) else 0,
             int(d.get("half_day_min_minutes",180)),
             int(d.get("full_day_min_minutes",390)),
             sid))
        conn.commit()
        return jsonify({"success":True})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})
    finally: conn.close()

@app.route("/shifts/delete/<int:sid>", methods=["POST"])
@amgr
def delete_shift(sid):
    conn = get_db()
    conn.execute("DELETE FROM shifts WHERE id=?", (sid,))
    conn.commit(); conn.close()
    return jsonify({"success":True})

@app.route("/shifts/assign", methods=["POST"])
@amgr
def assign_shift():
    d = request.json; conn = get_db()
    try:
        shift_mode = d.get("shift_mode", "fixed")
        for emp_code in d.get("emp_codes",[]):
            conn.execute("""INSERT INTO employee_shifts
                (emp_code,shift_id,shift_mode,effective_from,assigned_by,assigned_on)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(emp_code) DO UPDATE SET
                shift_id=excluded.shift_id,
                shift_mode=excluded.shift_mode,
                effective_from=excluded.effective_from,
                assigned_by=excluded.assigned_by,
                assigned_on=excluded.assigned_on""",
                (emp_code, int(d["shift_id"]), shift_mode,
                 d.get("effective_from", date.today().strftime("%Y-%m-%d")),
                 session.get("name","HR"), datetime.now().strftime("%Y-%m-%d %H:%M")))
        conn.commit()
        return jsonify({"success":True,"assigned":len(d.get("emp_codes",[]))})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})
    finally: conn.close()

@app.route("/shifts/assign-excel", methods=["POST"])
@amgr
def assign_shift_excel():
    """Upload Excel to bulk assign shifts"""
    if "file" not in request.files: return jsonify({"success":False,"error":"No file"})
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(request.files["file"].read()))
        ws = wb.active
        # Auto detect headers
        hdrs = [str(c.value).strip().lower().replace(" ","_") if c.value else "" for c in ws[1]]
        if "emp_code" not in hdrs:
            hdrs = [str(c.value).strip().lower().replace(" ","_") if c.value else "" for c in ws[2]]
            data_start = 3
        else:
            data_start = 2
        conn = get_db(); assigned = 0; errors = []
        for row in ws.iter_rows(min_row=data_start, values_only=True):
            if not any(row): continue
            d = dict(zip(hdrs, row))
            emp_code = str(d.get("emp_code","") or "").strip()
            shift_code = str(d.get("shift_code","") or d.get("shift","") or "").strip()
            if not emp_code or not shift_code: continue
            shift = conn.execute("SELECT id FROM shifts WHERE shift_code=? OR shift_name=?",
                                (shift_code,shift_code)).fetchone()
            if not shift: errors.append(f"{emp_code}: shift '{shift_code}' not found"); continue
            conn.execute("""INSERT INTO employee_shifts (emp_code,shift_id,effective_from,assigned_by,assigned_on)
                VALUES (?,?,?,?,?)
                ON CONFLICT(emp_code) DO UPDATE SET shift_id=excluded.shift_id,
                assigned_by=excluded.assigned_by,assigned_on=excluded.assigned_on""",
                (emp_code,shift["id"],date.today().strftime("%Y-%m-%d"),
                 session.get("name","HR"),datetime.now().strftime("%Y-%m-%d %H:%M")))
            assigned+=1
        conn.commit(); conn.close()
        return jsonify({"success":True,"assigned":assigned,"errors":errors[:5]})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})

@app.route("/shifts/get-assignment/<emp_code>")
@amgr
def get_shift_assignment(emp_code):
    conn = get_db()
    shift = conn.execute("""SELECT s.*, es.effective_from FROM shifts s
        JOIN employee_shifts es ON s.id=es.shift_id
        WHERE es.emp_code=?""", (emp_code,)).fetchone()
    conn.close()
    return jsonify(dict(shift)) if shift else jsonify({})


# ─── EMPLOYEE WISE ATTENDANCE EXPORT ────────────────


@app.route("/export/attendance-detail-all/<int:m>/<int:y>")
@amgr
def exp_att_detail_all(m, y):
    """Export ALL employees attendance — horizontal layout matching PDF sample, single sheet, emp_code sorted"""
    import openpyxl, calendar as _cal
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from datetime import date as dt_cls

    cat  = request.args.get("cat","")
    dept = request.args.get("dept","")
    conn = get_db()

    sql = "SELECT * FROM employees WHERE status='Active'"
    params = []
    if cat == "Staff":      sql += " AND category='Staff'"
    elif cat == "NonStaff": sql += " AND category!='Staff'"
    if dept: sql += " AND department=?"; params.append(dept)
    sql += " ORDER BY CAST(emp_code AS INTEGER) ASC"
    emps = conn.execute(sql, params).fetchall()

    all_att = conn.execute("""SELECT * FROM attendance
        WHERE strftime('%m',att_date)=? AND strftime('%Y',att_date)=?""",
        (f"{m:02d}", str(y))).fetchall()
    att_by_emp = {}
    for r in all_att:
        ec = r["emp_code"]
        if ec not in att_by_emp: att_by_emp[ec] = {}
        att_by_emp[ec][r["att_date"]] = dict(r)

    hols = {h["holiday_date"] for h in conn.execute(
        "SELECT holiday_date FROM holidays WHERE strftime('%m',holiday_date)=? AND strftime('%Y',holiday_date)=?",
        (f"{m:02d}", str(y))).fetchall()}
    wb  = openpyxl.Workbook()
    ws  = wb.active
    ws.title = f"{MONTHS[m-1]} {y}"

    month_days = _cal.monthrange(y, m)[1]
    day_abbr   = ["M","T","W","Th","F","St","S"]  # Mon=0..Sun=6

    def mk_border(color="CCCCCC", style="thin"):
        s = Side(style=style, color=color)
        return Border(left=s, right=s, top=s, bottom=s)

    thin   = mk_border()
    F_TITLE = PatternFill("solid", fgColor="0052CC")
    F_EMP   = PatternFill("solid", fgColor="0E3460")
    F_DAY   = PatternFill("solid", fgColor="1E4FA3")
    F_SUN   = PatternFill("solid", fgColor="3A6BC4")
    F_LABEL = PatternFill("solid", fgColor="243B55")
    F_SUMM  = PatternFill("solid", fgColor="1A2E4A")
    F_P     = PatternFill("solid", fgColor="C6EFCE")
    F_A     = PatternFill("solid", fgColor="FFC7CE")
    F_WO    = PatternFill("solid", fgColor="EEEEEE")

    total_col = month_days + 2  # A=label, B..=day1.., last=Total

    # Pre-fetch weekly off for all employees in ONE query
    wo_rows = conn2.execute("SELECT emp_code, weekly_off FROM employees WHERE status='Active'").fetchall() if False else []
    # Actually get from emps directly
    wo_map = {}
    for e in emps:
        wo_val = e["weekly_off"] if "weekly_off" in e.keys() else "Sunday"
        days_map = {"Monday":0,"Tuesday":1,"Wednesday":2,"Thursday":3,"Friday":4,"Saturday":5,"Sunday":6}
        wo_map[e["emp_code"]] = days_map.get(wo_val, 6)
    conn.close()

    cur_row = 1

    for emp_idx, emp in enumerate(emps):
        ec       = emp["emp_code"]
        att_dict = att_by_emp.get(ec, {})
        today    = dt_cls.today()
        emp_wo_day = wo_map.get(ec, 6)  # weekly off weekday number

        # Title row
        last_ltr = get_column_letter(total_col)
        ws.merge_cells(f"A{cur_row}:{last_ltr}{cur_row}")
        c1 = ws[f"A{cur_row}"]
        c1.value     = f"VIJAYSHRI PACKAGING LTD. — Attendance Register {MONTHS[m-1]} {y}"
        c1.font      = Font(bold=True, size=11, color="FFFFFF")
        c1.fill      = F_TITLE
        c1.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[cur_row].height = 18
        cur_row += 1

        # Emp info row
        ws.merge_cells(f"A{cur_row}:{last_ltr}{cur_row}")
        c2 = ws[f"A{cur_row}"]
        c2.value     = f"Employee: {ec} : {emp['emp_name']}    Department: {emp['department'] or '—'}  |  Category: {emp['category']}"
        c2.font      = Font(bold=True, size=10, color="FFFFFF")
        c2.fill      = F_EMP
        c2.alignment = Alignment(horizontal="left", vertical="center")
        ws.row_dimensions[cur_row].height = 15
        cur_row += 1

        # Days header: col A = "Days", col B..= day number + abbr, last = "Total"
        ws.cell(cur_row, 1, "Days").font = Font(bold=True, size=9, color="FFFFFF")
        ws.cell(cur_row, 1).fill = F_DAY
        ws.cell(cur_row, 1).alignment = Alignment(horizontal="center")
        ws.cell(cur_row, 1).border = thin

        for day in range(1, month_days + 1):
            d_obj  = dt_cls(y, m, day)
            is_sun = (d_obj.weekday() == emp_wo_day)
            abbr   = day_abbr[d_obj.weekday()]
            col    = day + 1
            cell   = ws.cell(cur_row, col)
            cell.value     = f"{day}\n{abbr}"
            cell.font      = Font(bold=True, size=8, color="FFFFFF")
            cell.fill      = F_SUN if is_sun else F_DAY
            cell.alignment = Alignment(horizontal="center", wrap_text=True, vertical="center")
            cell.border    = thin

        tc = ws.cell(cur_row, total_col, "Total")
        tc.font = Font(bold=True, size=9, color="FFFFFF")
        tc.fill = F_DAY
        tc.alignment = Alignment(horizontal="center")
        tc.border = thin
        ws.row_dimensions[cur_row].height = 22
        cur_row += 1

        # Pre-compute per-day data
        d_st={};d_in={};d_out={};d_dur={};d_late={};d_early={};d_ot={}
        p_cnt=0;a_cnt=0;tot_ot=0;tot_late=0

        for day in range(1, month_days + 1):
            dt_str = f"{y}-{m:02d}-{day:02d}"
            d_obj  = dt_cls(y, m, day)
            row    = att_dict.get(dt_str, {})
            st     = row.get("status","") or ""
            in_t   = row.get("in_time","") or ""
            out_t  = row.get("out_time","") or ""
            wmin   = row.get("working_minutes",0) or 0
            late   = row.get("late_minutes",0) or 0
            ot     = row.get("ot_minutes",0) or 0
            early  = row.get("short_minutes",0) or 0
            is_hd  = row.get("is_half_day",0) or 0

            if not st:
                if d_obj.weekday()==emp_wo_day:     st="WO"
                elif dt_str in hols:       st="Holiday"
                elif d_obj<=today:         st="Absent"

            hp = bool(in_t)
            if st=="Present":
                disp = "P½" if is_hd else "P"
            elif st=="Absent":  disp="A"
            elif st in("WO",""): disp="WOP" if hp else "WO"
            elif st=="WOP":     disp="WOP"
            elif st=="Holiday": disp="HP" if hp else "H"
            elif st=="Leave":   disp="L"
            else:               disp=st or ""

            d_st[day]    = disp
            d_in[day]    = in_t
            d_out[day]   = out_t
            d_dur[day]   = f"{wmin//60:02d}:{wmin%60:02d}" if wmin else ""
            d_late[day]  = f"{late//60}:{late%60:02d}" if late else ""
            d_early[day] = f"{early//60}:{early%60:02d}" if early else ""
            d_ot[day]    = f"{ot//60}:{ot%60:02d}" if ot else ""

            if disp=="P":   p_cnt += 0.5 if is_hd else 1
            elif disp=="A": a_cnt += 1
            tot_ot += ot; tot_late += late

        # Data rows: Status, In Time, Out Time, Duration, Late By, Early By, OT
        rows_def = [
            ("Status",   d_st,    lambda v,day: (
                PatternFill("solid", fgColor="FFD9E8") if v=="P½" else
                F_P if v=="P" else F_A if v=="A" else F_WO if v in("WO","H","") else None,
                Font(bold=True,size=8,color="8B0045") if v=="P½" else
                Font(bold=True,size=8,color="276221") if v=="P" else
                Font(bold=True,size=8,color="9C0006") if v=="A" else
                Font(size=8)
            )),
            ("In Time",  d_in,    None),
            ("Out Time", d_out,   None),
            ("Duration", d_dur,   None),
            ("Late By",  d_late,  None),
            ("Early By", d_early, None),
            ("OT",       d_ot,    None),
        ]

        for row_label, day_vals, styler in rows_def:
            lc = ws.cell(cur_row, 1, row_label)
            lc.font = Font(bold=True, size=9, color="FFFFFF")
            lc.fill = F_LABEL
            lc.alignment = Alignment(horizontal="left", vertical="center")
            lc.border = thin

            for day in range(1, month_days+1):
                col  = day + 1
                val  = day_vals.get(day,"")
                cell = ws.cell(cur_row, col, val)
                cell.font      = Font(size=8)
                cell.alignment = Alignment(horizontal="center", vertical="center")
                cell.border    = thin
                if styler:
                    fill, font = styler(val, day)
                    if fill: cell.fill = fill
                    if font: cell.font = font
                # Sunday light bg for empty cells
                if dt_cls(y,m,day).weekday()==emp_wo_day and not val:
                    cell.fill = PatternFill("solid", fgColor="F0F4FF")

            # Total column
            tot_val = ""
            if row_label=="Status":
                tot_val = f"P:{int(p_cnt) if p_cnt==int(p_cnt) else p_cnt}\nA:{a_cnt}"
            elif row_label=="OT":
                tot_val = f"{tot_ot//60}:{tot_ot%60:02d}" if tot_ot else ""
            elif row_label=="Late By":
                tot_val = f"{tot_late//60}:{tot_late%60:02d}" if tot_late else ""
            elif row_label=="Duration":
                tot_wmin = sum(
                    (int(v.split(":")[0])*60 + int(v.split(":")[1]))
                    for v in day_vals.values() if v and ":" in v
                )
                tot_val = f"{tot_wmin//60}:{tot_wmin%60:02d}" if tot_wmin else ""

            tcell = ws.cell(cur_row, total_col, tot_val)
            tcell.font = Font(bold=True, size=8, color="FFFFFF")
            tcell.fill = F_SUMM
            tcell.alignment = Alignment(horizontal="center", wrap_text=True, vertical="center")
            tcell.border = thin
            ws.row_dimensions[cur_row].height = 14
            cur_row += 1

        # 3 blank rows gap
        if emp_idx < len(emps) - 1:
            cur_row += 3

    # Column widths
    ws.column_dimensions["A"].width = 11
    for day in range(1, month_days + 1):
        ws.column_dimensions[get_column_letter(day + 1)].width = 5.5
    ws.column_dimensions[get_column_letter(total_col)].width = 10

    # Freeze panes
    ws.freeze_panes = "B1"

    return xlresp(wb, f"Attendance_All_{MONTHS[m-1]}_{y}.xlsx")



@app.route("/export/attendance-employee/<emp_code>/<int:m>/<int:y>")
@amgr
def exp_att_employee(emp_code, m, y):
    """Export detailed day-by-day attendance for one employee"""
    conn = get_db()
    emp = conn.execute("SELECT * FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
    if not emp: conn.close(); return "Employee not found", 404

    att = conn.execute("""SELECT * FROM attendance 
        WHERE emp_code=? AND strftime('%m',att_date)=? AND strftime('%Y',att_date)=?
        ORDER BY att_date""",
        (emp_code, f"{m:02d}", str(y))).fetchall()
    conn.close()

    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = f"{MONTHS[m-1]} {y}"

    # Header
    ws.merge_cells("A1:L1")
    ws["A1"] = "VIJAYSHRI PACKAGING LTD."
    ws["A1"].font = Font(bold=True, size=14, color="FFFFFF")
    ws["A1"].fill = PatternFill("solid", fgColor="0052CC")
    ws["A1"].alignment = Alignment(horizontal="center")

    ws.merge_cells("A2:L2")
    ws["A2"] = f"Attendance Detail — {emp['emp_name']} ({emp_code}) | {MONTHS[m-1]} {y}"
    ws["A2"].font = Font(bold=True, size=11, color="FFFFFF")
    ws["A2"].fill = PatternFill("solid", fgColor="0096DC")
    ws["A2"].alignment = Alignment(horizontal="center")

    # Employee info
    ws["A3"] = "Category:"; ws["B3"] = emp["category"]
    ws["C3"] = "Department:"; ws["D3"] = emp["department"] or "—"
    ws["E3"] = "Designation:"; ws["F3"] = emp["designation"] or "—"

    # Column headers
    hdrs = ["Date","Day","Punch In","Punch Out","Working Hrs","Status","Late (min)","OT (min)","Half Day","Shift","Remarks"]
    for col, h in enumerate(hdrs, 1):
        cell = ws.cell(row=5, column=col, value=h)
        cell.font = Font(bold=True, color="FFFFFF", size=10)
        cell.fill = PatternFill("solid", fgColor="1E3A5F")
        cell.alignment = Alignment(horizontal="center")

    # Status colors
    status_colors = {
        "Present": "DCFCE7", "Absent": "FEE2E2", "WO": "F1F5F9",
        "WOP": "FEF3C7", "Leave": "EDE9FE", "Holiday": "DBEAFE", "Half Day": "FFF7ED"
    }

    # Data rows
    days = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    import calendar
    month_days = calendar.monthrange(y, m)[1]
    att_dict = {r["att_date"]: dict(r) for r in att}

    present=0; absent=0; wo=0; wop=0; leave=0; hd=0; ot_total=0; late_total=0; duration_total=0

    for day in range(1, month_days+1):
        dt_str = f"{y}-{m:02d}-{day:02d}"
        from datetime import date as dt_cls
        d = dt_cls(y, m, day)
        day_name = days[d.weekday()]
        row = att_dict.get(dt_str, {})
        status = row.get("status", "") or ""
        in_t   = row.get("in_time", "") or ""
        out_t  = row.get("out_time", "") or ""
        wmin   = row.get("working_minutes", 0) or 0
        late   = row.get("late_minutes", 0) or 0
        ot     = row.get("ot_minutes", 0) or 0
        is_hd  = row.get("is_half_day", 0) or 0
        shift  = row.get("shift_name", "") or ""

        # Holiday/WO check — even if no attendance record
        if not status:
            if d.weekday() == get_emp_weekly_off_num(emp_code):
                status = "WO"
            else:
                hol_check = conn.execute(
                    "SELECT title FROM holidays WHERE holiday_date=?", (dt_str,)).fetchone()
                if hol_check:
                    status = "Holiday"
                elif d < date.today():
                    status = "Absent"
                else:
                    status = "—"

        # Display status
        has_punch = bool(in_t)
        if status == "Present":   disp_st = "P"
        elif status == "Absent":  disp_st = "A"
        elif status == "WO":      disp_st = "WOP" if has_punch else "WO"
        elif status == "WOP":     disp_st = "WOP"
        elif status == "Holiday": disp_st = "HP" if has_punch else "H"
        elif status == "Leave":   disp_st = "L"
        elif status == "Half Day":disp_st = "HD"
        else:                     disp_st = status or "—"

        wh = f"{wmin//60:02d}:{wmin%60:02d}" if wmin else "—"
        ot_fmt = f"{ot//60:02d}:{ot%60:02d}" if ot else "—"

        r_data = [dt_str, day_name, in_t or "—", out_t or "—", wh, disp_st,
                  late if late else "—", ot_fmt,
                  "Yes" if is_hd else "—", shift or "—", ""]

        row_num = day + 5
        for col, val in enumerate(r_data, 1):
            cell = ws.cell(row=row_num, column=col, value=val)
            cell.alignment = Alignment(horizontal="center")
            cell.font = Font(size=9)

        # Status cell color
        st_cell = ws.cell(row=row_num, column=6)
        color_map = {
            "P":"C6EFCE","A":"FFC7CE","WO":"E2EFDA","WOP":"FEF3C7",
            "HP":"DBEAFE","H":"DBEAFE","L":"EDE9FE","HD":"FFF7ED"
        }
        bg = color_map.get(disp_st, "FFFFFF")
        st_cell.fill = PatternFill("solid", fgColor=bg)
        if disp_st == "A":
            st_cell.font = Font(bold=True, color="9C0006", size=9)
        elif disp_st == "P":
            st_cell.font = Font(bold=True, color="276221", size=9)
        else:
            st_cell.font = Font(bold=True, size=9)

        # Count — WOP/HP NOT in present, only in OT track
        if disp_st == "P":
            present += 0.5 if is_hd else 1
        elif disp_st == "A":  absent += 1
        elif disp_st == "WO": wo += 1
        elif disp_st == "WOP": wop += 1  # Weekly Off Present — OT only
        elif disp_st == "H":  wo += 1   # Holiday no punch — like WO
        elif disp_st == "HP": wop += 1  # Holiday Present — OT only
        elif disp_st == "L":  leave += 1
        if is_hd: hd += 1
        ot_total += ot
        late_total += late
        duration_total += wmin

    # Summary row
    sr = month_days + 7
    ws.merge_cells(f"A{sr}:B{sr}")
    ws[f"A{sr}"] = "SUMMARY"
    ws[f"A{sr}"].font = Font(bold=True, color="FFFFFF")
    ws[f"A{sr}"].fill = PatternFill("solid", fgColor="0052CC")

    dur_h = duration_total // 60; dur_m = duration_total % 60
    ot_h = ot_total // 60; ot_m = ot_total % 60
    summary = [("Present Days", present), ("Absent", absent), ("Week Off", wo),
               ("WOP", wop), ("Leave", leave), ("Half Days", hd),
               ("Total Duration", f"{dur_h:02d}:{dur_m:02d} hrs"),
               ("Total OT", f"{ot_h:02d}:{ot_m:02d} hrs"), ("Total Late (min)", late_total)]
    for i, (k,v) in enumerate(summary):
        c = (i % 4) * 2 + 1
        r = sr + (i // 4)
        ws.cell(row=r, column=c, value=k).font = Font(bold=True, size=9)
        ws.cell(row=r, column=c+1, value=v).font = Font(size=9)

    # Column widths
    widths = [12,6,10,10,10,10,9,9,9,12,14]
    for i,w in enumerate(widths,1):
        ws.column_dimensions[get_column_letter(i)].width = w

    return xlresp(wb, f"Attendance_{emp_code}_{MONTHS[m-1]}_{y}.xlsx")

@app.route("/export/attendance-range/<emp_code>", methods=["POST"])
@amgr
def exp_att_range(emp_code):
    """Export attendance for custom date range"""
    d = request.json
    from_date = d.get("from_date")
    to_date   = d.get("to_date")
    conn = get_db()
    emp = conn.execute("SELECT * FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
    if not emp: conn.close(); return jsonify({"success": False, "error": "Not found"})
    att = conn.execute("""SELECT * FROM attendance 
        WHERE emp_code=? AND att_date>=? AND att_date<=?
        ORDER BY att_date""", (emp_code, from_date, to_date)).fetchall()
    conn.close()

    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    wb = openpyxl.Workbook(); ws = wb.active
    ws.title = "Attendance"
    ws.append(["Date","Day","In Time","Out Time","Working Hrs","Status","Late(min)","OT(min)","Half Day"])
    days = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
    for r in att:
        from datetime import date as dt_cls
        d2 = dt_cls.fromisoformat(r["att_date"])
        wmin = r["working_minutes"] or 0
        ws.append([r["att_date"], days[d2.weekday()],
                   r["in_time"] or "—", r["out_time"] or "—",
                   f"{wmin//60}:{wmin%60:02d}" if wmin else "—",
                   r["status"] or "—",
                   r["late_minutes"] or 0, r["ot_minutes"] or 0,
                   "Yes" if r["is_half_day"] else "No"])
    return xlresp(wb, f"Attendance_{emp_code}_{from_date}_to_{to_date}.xlsx")


# ─── SHIFT GROUPS ────────────────────────────────────

@app.route("/shift-groups")
@amgr
def shift_groups():
    conn = get_db()
    groups = conn.execute("SELECT * FROM shift_groups ORDER BY group_name").fetchall()
    all_shifts = conn.execute("SELECT * FROM shifts WHERE is_active=1 ORDER BY shift_name").fetchall()
    # For each group, get its shifts
    groups_with_shifts = []
    for g in groups:
        members = conn.execute("""SELECT s.* FROM shifts s
            JOIN shift_group_members sgm ON s.id=sgm.shift_id
            WHERE sgm.group_id=?""",(g["id"],)).fetchall()
        groups_with_shifts.append({"group": dict(g), "shifts": [dict(s) for s in members]})
    # Employees assigned to groups
    assigned = conn.execute("""SELECT esg.*, 
        COALESCE(e.emp_name,'Unknown') as emp_name,
        COALESCE(e.department,'—') as department,
        COALESCE(e.category,'—') as category,
        COALESCE(sg.group_name,'—') as group_name
        FROM employee_shift_groups esg
        LEFT JOIN employees e ON esg.emp_code=e.emp_code
        LEFT JOIN shift_groups sg ON esg.group_id=sg.id
        ORDER BY e.emp_name""").fetchall()
    employees = conn.execute("SELECT emp_code,emp_name,department,category FROM employees WHERE status='Active' ORDER BY category,emp_name").fetchall()
    conn.close()
    return render_template("shift_groups.html",
        groups=groups_with_shifts, all_shifts=all_shifts,
        assigned=assigned, employees=employees)

@app.route("/shift-groups/add", methods=["POST"])
@amgr
def add_shift_group():
    d = request.json; conn = get_db()
    try:
        conn.execute("INSERT INTO shift_groups (group_name,description,is_active,created_on) VALUES (?,?,1,date('now'))",
                    (d["group_name"], d.get("description","")))
        conn.commit()
        gid = conn.execute("SELECT id FROM shift_groups WHERE group_name=?", (d["group_name"],)).fetchone()["id"]
        # Add shifts to group
        for sid in d.get("shift_ids",[]):
            conn.execute("INSERT OR IGNORE INTO shift_group_members (group_id,shift_id) VALUES (?,?)", (gid,int(sid)))
        conn.commit()
        return jsonify({"success":True,"group_id":gid})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})
    finally: conn.close()

@app.route("/shift-groups/edit/<int:gid>", methods=["POST"])
@amgr
def edit_shift_group(gid):
    d = request.json; conn = get_db()
    try:
        conn.execute("UPDATE shift_groups SET group_name=?,description=?,is_active=? WHERE id=?",
                    (d["group_name"],d.get("description",""),1 if d.get("is_active",True) else 0,gid))
        # Update members
        conn.execute("DELETE FROM shift_group_members WHERE group_id=?", (gid,))
        for sid in d.get("shift_ids",[]):
            conn.execute("INSERT OR IGNORE INTO shift_group_members (group_id,shift_id) VALUES (?,?)", (gid,int(sid)))
        conn.commit()
        return jsonify({"success":True})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})
    finally: conn.close()

@app.route("/shift-groups/delete/<int:gid>", methods=["POST"])
@amgr
def delete_shift_group(gid):
    conn = get_db()
    conn.execute("DELETE FROM shift_group_members WHERE group_id=?", (gid,))
    conn.execute("DELETE FROM employee_shift_groups WHERE group_id=?", (gid,))
    conn.execute("DELETE FROM shift_groups WHERE id=?", (gid,))
    conn.commit(); conn.close()
    return jsonify({"success":True})

@app.route("/shift-groups/assign", methods=["POST"])
@amgr
def assign_shift_group():
    d = request.json; conn = get_db()
    try:
        for emp_code in d.get("emp_codes",[]):
            conn.execute("""INSERT INTO employee_shift_groups (emp_code,group_id,assigned_by,assigned_on)
                VALUES (?,?,?,?)
                ON CONFLICT(emp_code) DO UPDATE SET group_id=excluded.group_id,
                assigned_by=excluded.assigned_by, assigned_on=excluded.assigned_on""",
                (emp_code, int(d["group_id"]),
                 session.get("name","HR"), datetime.now().strftime("%Y-%m-%d %H:%M")))
        conn.commit()
        return jsonify({"success":True,"assigned":len(d.get("emp_codes",[]))})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})
    finally: conn.close()

@app.route("/shift-groups/get-for-emp/<emp_code>")
@amgr
def get_shift_group_for_emp(emp_code):
    conn = get_db()
    grp = conn.execute("""SELECT sg.*, esg.assigned_on FROM shift_groups sg
        JOIN employee_shift_groups esg ON sg.id=esg.group_id
        WHERE esg.emp_code=?""", (emp_code,)).fetchone()
    conn.close()
    return jsonify(dict(grp)) if grp else jsonify({})

def detect_shift_from_group(emp_code, in_time, conn):
    """
    Detect shift using eSSL detect_shift_by_punch from employee's shift group.
    """
    try:
        grp = conn.execute("""SELECT sgm.shift_id FROM employee_shift_groups esg
            JOIN shift_group_members sgm ON esg.group_id=sgm.group_id
            WHERE esg.emp_code=?""", (emp_code,)).fetchall()
    except Exception:
        s, _ = get_shift_for_emp(emp_code, conn=conn)
        return s

    if not grp:
        s, _ = get_shift_for_emp(emp_code, conn=conn)
        return s

    shift_ids = [r["shift_id"] for r in grp]
    if not shift_ids:
        s, _ = get_shift_for_emp(emp_code, conn=conn)
        return s

    placeholders = ",".join("?"*len(shift_ids))
    shifts = conn.execute(
        f"SELECT * FROM shifts WHERE id IN ({placeholders}) AND is_active=1",
        shift_ids).fetchall()
    if not shifts:
        s, _ = get_shift_for_emp(emp_code, conn=conn)
        return s

    return detect_shift_by_punch(in_time, [dict(s) for s in shifts])


def pull_employees_from_machine():
    """Pull employee list from biometric machine and add to software"""
    d = request.json
    ip       = d.get("ip","192.168.0.125")
    port     = int(d.get("port",4370))
    password = int(d.get("password",0))
    try:
        from zk import ZK
        zk  = ZK(ip, port=port, timeout=60, password=password, force_udp=False, ommit_ping=True)
        czk = zk.connect()
        czk.disable_device()
        users = czk.get_users()
        czk.enable_device()
        czk.disconnect()

        conn = get_db()
        added = 0; existing = 0
        for u in users:
            uid = str(u.user_id).strip()
            if not uid: continue
            # Check if employee already exists
            # Try all code formats
            emp_check, matched_uid = find_emp_by_machine_id(conn, uid)
            if emp_check:
                existing += 1
                continue
            # Add new employee from machine
            name = u.name.strip() if u.name else f"Employee {uid}"
            conn.execute("""INSERT OR IGNORE INTO employees 
                (emp_code, emp_name, category, status)
                VALUES (?,?,'Associate','Active')""", (uid, name))
            conn.execute("INSERT OR IGNORE INTO users (username,password,role,emp_id,name) VALUES (?,?,?,?,?)",
                        (uid.lower(), hp("Emp@123"), "employee", uid, name))
            added += 1
        conn.commit(); conn.close()
        return jsonify({"success":True, "added":added, "existing":existing,
                       "total":len(users)})
    except ImportError:
        return jsonify({"success":False, "error":"Run: pip install pyzk"})
    except Exception as e:
        return jsonify({"success":False, "error":str(e)})


# ─── ADVANCED REPORTS ────────────────────────────────

@app.route("/reports/attendance-advanced", methods=["POST"])
@amgr
def att_report_advanced():
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    d = request.json
    report_type = d.get("report_type","detailed")
    emp_code    = str(d.get("emp_code","") or "").strip()
    month       = int(d.get("month",0))
    year        = int(d.get("year", date.today().year))
    from_date   = str(d.get("from_date","") or "").strip()
    to_date     = str(d.get("to_date","") or "").strip()

    # Fix date format DD-MM-YYYY → YYYY-MM-DD if needed
    def fix_date(dt):
        if not dt or len(dt) < 8: return dt
        if dt[2] == '-' and dt[5] == '-':
            p = dt.split('-')
            return f"{p[2]}-{p[1]}-{p[0]}"
        return dt
    from_date = fix_date(from_date)
    to_date   = fix_date(to_date)

    print(f"[REPORT] type={report_type} emp={emp_code!r} month={month} year={year} from={from_date} to={to_date}")

    conn = get_db()
    # Build employee list
    emp_sql = "SELECT * FROM employees WHERE status='Active'"
    params  = []
    if emp_code:
        emp_sql += " AND emp_code=?"
        params.append(emp_code)
    emp_sql += " ORDER BY department, emp_name"
    emps = conn.execute(emp_sql, params).fetchall()

    # BULK PRELOAD — single query for all employees instead of N queries
    _att_sql = "SELECT * FROM attendance WHERE 1=1"
    _att_p   = []
    if from_date and to_date:
        _att_sql += " AND att_date>=? AND att_date<=?"
        _att_p  += [from_date, to_date]
    elif month > 0:
        _att_sql += " AND strftime('%m',att_date)=? AND strftime('%Y',att_date)=?"
        _att_p  += [f"{month:02d}", str(year)]
    else:
        _att_sql += " AND strftime('%Y',att_date)=?"
        _att_p.append(str(year))
    if emp_code:
        _att_sql += " AND emp_code=?"
        _att_p.append(emp_code)
    _all_att_rows = conn.execute(_att_sql + " ORDER BY emp_code, att_date", _att_p).fetchall()
    from collections import defaultdict as _dd
    _att_preload = _dd(list)
    for _r in _all_att_rows:
        _att_preload[_r["emp_code"]].append(_r)

    def get_att(ec):
        return _att_preload.get(ec, [])

    def get_holidays_in_range():
        """Get all holidays in report date range"""
        if from_date and to_date:
            return {r["holiday_date"]: r["title"] for r in conn.execute(
                "SELECT holiday_date,title FROM holidays WHERE holiday_date>=? AND holiday_date<=?",
                (from_date, to_date)).fetchall()}
        elif month > 0:
            return {r["holiday_date"]: r["title"] for r in conn.execute(
                "SELECT holiday_date,title FROM holidays WHERE strftime('%m',holiday_date)=? AND strftime('%Y',holiday_date)=?",
                (f"{month:02d}", str(year))).fetchall()}
        return {}

    holiday_map = get_holidays_in_range()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Attendance Report"
    thin = Side(style="thin", color="D0DCF0")
    bdr  = Border(**{s:Side(style="thin",color="D0DCF0") for s in ["left","right","top","bottom"]})

    period = f"{from_date} to {to_date}" if from_date else (MONTHS[month-1] if month else str(year))
    emp_label = emps[0]["emp_name"] if emp_code and emps else "All Employees"
    ws.merge_cells("A1:N1")
    ws["A1"] = f"VIJAYSHRI PACKAGING LTD. — Attendance {report_type.title()} | {period} | {emp_label}"
    ws["A1"].font = Font(bold=True,size=12,color="FFFFFF")
    ws["A1"].fill = PatternFill("solid",fgColor="0052CC")
    ws["A1"].alignment = Alignment(horizontal="center",vertical="center")
    ws.row_dimensions[1].height = 26

    if report_type == "detailed":
        hdrs = ["Emp Code","Name","Department","Category","Date","Day","Punch In","Punch Out","Working Hrs","Status","Late(min)","OT(min)","Half Day","Shift"]
        ws.append(hdrs)
        for c in ws[2]:
            c.font = Font(bold=True,size=9,color="FFFFFF")
            c.fill = PatternFill("solid",fgColor="1E3A5F")
            c.alignment = Alignment(horizontal="center")
        days = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"]
        # Build date range for filling missing days as Absent
        import calendar as _calx
        from datetime import date as _dtx, timedelta as _tdx
        if from_date and to_date:
            _fd = _dtx.fromisoformat(from_date); _td_ = _dtx.fromisoformat(to_date)
            date_range = []
            _cur = _fd
            while _cur <= _td_:
                date_range.append(_cur.strftime("%Y-%m-%d")); _cur += _tdx(days=1)
        elif month > 0:
            total_days = _calx.monthrange(year, month)[1]
            date_range = [_dtx(year,month,d).strftime("%Y-%m-%d") for d in range(1,total_days+1)]
        else:
            date_range = []

        for emp in emps:
            rows = get_att(emp["emp_code"])
            # Build dict for quick lookup
            att_map = {r["att_date"]: dict(r) for r in rows}

            # Iterate all dates in range — generate Absent for missing working days
            iter_dates = date_range if date_range else [r["att_date"] for r in rows]
            for att_date_r in iter_dates:
                try:
                    d2 = _dtx.fromisoformat(att_date_r)
                    day_name = days[d2.weekday()]
                    is_sun = (d2.weekday() == get_emp_weekly_off_num(emp["emp_code"]))
                except:
                    day_name = "—"; is_sun = False

                r = att_map.get(att_date_r)

                if r is None:
                    # No record for this day
                    if is_sun:
                        disp_st = "WO"; st = "WO"
                        wm = 0; ot = 0; has_punch = False
                    elif att_date_r in holiday_map:
                        disp_st = "H"; st = "Holiday"
                        wm = 0; ot = 0; has_punch = False
                    else:
                        disp_st = "A"; st = "Absent"
                        wm = 0; ot = 0; has_punch = False
                else:
                    wm = r["working_minutes"] or 0
                    ot = r["ot_minutes"] or 0
                    st = r["status"] or ""
                    has_punch = bool(r["in_time"])
                    if not st or st == "":
                        if att_date_r in holiday_map: st = "Holiday"
                        elif r["in_time"]: st = "Present"
                        else: st = "Absent"
                    elif st == "Present" and att_date_r in holiday_map and has_punch:
                        st = "Holiday"
                    if st == "Present":   disp_st = "P"
                    elif st == "Absent":  disp_st = "A"
                    elif st == "WOP":     disp_st = "WOP"
                    elif st == "WO":      disp_st = "WOP" if has_punch else "WO"
                    elif st == "Holiday": disp_st = "HP" if has_punch else "H"
                    elif st == "Leave":   disp_st = "L"
                    else:                 disp_st = st or "A"

                # Get values safely (r may be None for missing days)
                in_time  = (r["in_time"]  if r else "") or "—"
                out_time = (r["out_time"] if r else "") or "—"
                late_min = (r["late_minutes"] if r else 0) or 0
                is_hd    = (r["is_half_day"] if r else 0) or 0
                sh_name  = (r.get("shift_name","") if r else "") or "—"
                ws.append([emp["emp_code"],emp["emp_name"],emp["department"] or "—",emp["category"],
                    att_date_r, day_name, in_time, out_time,
                    f"{wm//60:02d}:{wm%60:02d}" if wm else "—", disp_st,
                    late_min,
                    f"{ot//60:02d}:{ot%60:02d}" if ot else "—",
                    "Yes" if is_hd else "No", sh_name])

                # Color status cell
                row_num = ws.max_row
                st_cell = ws.cell(row=row_num, column=10)
                st_cell.alignment = Alignment(horizontal="center")
                if disp_st == "A":
                    st_cell.fill = PatternFill("solid", fgColor="FFC7CE")
                    st_cell.font = Font(bold=True, color="9C0006", size=9)
                elif disp_st == "P":
                    st_cell.fill = PatternFill("solid", fgColor="C6EFCE")
                    st_cell.font = Font(bold=True, color="276221", size=9)
                elif disp_st in ("WOP","WO"):
                    st_cell.fill = PatternFill("solid", fgColor="E2EFDA")
                    st_cell.font = Font(bold=True, color="375623", size=9)
                elif disp_st in ("HP","H"):
                    st_cell.fill = PatternFill("solid", fgColor="FFEB9C")
                    st_cell.font = Font(bold=True, color="9C5700", size=9)
        for i,w in enumerate([10,22,16,10,12,6,10,10,10,10,9,9,9,12],1):
            ws.column_dimensions[get_column_letter(i)].width = w
    else:
        hdrs = ["Emp Code","Name","Department","Category","Working Days","Present","Absent","Leave","WO","WOP","Half Days","OT Hours","Late Days","Total Hrs"]
        ws.append(hdrs)
        for c in ws[2]:
            c.font = Font(bold=True,size=9,color="FFFFFF")
            c.fill = PatternFill("solid",fgColor="1E3A5F")
            c.alignment = Alignment(horizontal="center")
        for emp in emps:
            rows = get_att(emp["emp_code"])
            wd = get_wd(year, month if month else date.today().month, emp["category"])
            present = sum(0.5 if r["is_half_day"] else 1 for r in rows if r["status"] not in ("Absent","Leave","WO"))
            twm = sum(r["working_minutes"] or 0 for r in rows)
            ws.append([emp["emp_code"],emp["emp_name"],emp["department"] or "—",emp["category"],wd,present,
                sum(1 for r in rows if r["status"]=="Absent"),sum(1 for r in rows if r["status"]=="Leave"),
                sum(1 for r in rows if r["status"]=="WO"),sum(1 for r in rows if r["status"]=="WOP"),
                sum(1 for r in rows if r["is_half_day"]),round(sum(r["ot_minutes"] or 0 for r in rows)/60,2),
                sum(1 for r in rows if (r["late_minutes"] or 0)>0),f"{twm//60}:{twm%60:02d}"])
        for i,w in enumerate([10,22,16,10,10,9,9,9,8,8,9,10,9,12],1):
            ws.column_dimensions[get_column_letter(i)].width = w

    for row in ws.iter_rows(min_row=3):
        for cell in row:
            cell.border = bdr
            cell.font   = Font(size=9)
            cell.alignment = Alignment(horizontal="center",vertical="center")
            if cell.row % 2 == 0:
                cell.fill = PatternFill("solid",fgColor="F8FAFC")
    conn.close()
    fname = f"Attendance_{report_type.title()}_{period.replace(' ','_').replace(':','')}.xlsx"
    return xlresp(wb, fname)




@app.route("/test/emp-check/<emp_code>")
def test_emp_check(emp_code):
    conn = get_db()
    emp = conn.execute("SELECT emp_code, emp_name FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
    all_emps = conn.execute("SELECT emp_code FROM employees LIMIT 5").fetchall()
    conn.close()
    return jsonify({
        "searched": emp_code,
        "found": dict(emp) if emp else None,
        "sample_codes": [e["emp_code"] for e in all_emps]
    })


# ─── ATTENDANCE REGISTER REPORTS ─────────────────────

@app.route("/reports/attendance-register", methods=["POST"])
@amgr
def att_register_report():
    """Generate attendance register in sample format - Detailed or In/Out"""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    import calendar as cal_mod

    d           = request.json
    rpt_type    = d.get("rpt_type","detailed")   # detailed / inout
    scope       = d.get("scope","monthly")        # monthly / yearly
    emp_code    = str(d.get("emp_code","") or "").strip()
    dept        = str(d.get("dept","") or "").strip()
    month       = int(d.get("month", date.today().month))
    year        = int(d.get("year",  date.today().year))

    conn = get_db()

    # Build employee list
    emp_sql = "SELECT * FROM employees WHERE status='Active'"
    ep = []
    if emp_code: emp_sql += " AND emp_code=?"; ep.append(emp_code)
    if dept:     emp_sql += " AND department=?"; ep.append(dept)
    emp_sql += " ORDER BY department, emp_name"
    emps = conn.execute(emp_sql, ep).fetchall()

    wb = openpyxl.Workbook()
    wb.remove(wb.active)  # remove default sheet

    thin  = Side(style="thin",   color="BBBBBB")
    thick = Side(style="medium", color="0052CC")
    bdr   = Border(left=thin,right=thin,top=thin,bottom=thin)
    bdr_t = Border(left=thick,right=thick,top=thick,bottom=thick)

    DAY_NAMES = ["S","M","T","W","Th","F","St"]  # Sun=0 in Python weekday() is Mon=0
    # Python: Mon=0,Tue=1,Wed=2,Thu=3,Fri=4,Sat=5,Sun=6
    PY_TO_DISPLAY = ["M","T","W","Th","F","St","S"]

    COLORS = {
        "header_dark":  "0052CC",
        "header_mid":   "0096DC",
        "header_light": "DBEAFE",
        "present":      "DCFCE7",
        "absent":       "FEE2E2",
        "wo":           "FEF9C3",
        "wop":          "EDE9FE",
        "row_label":    "1E3A5F",
        "alt_row":      "F0F9FF",
        "white":        "FFFFFF",
    }

    def mk_cell(ws, row, col, val, bold=False, size=9, color="000000", bg=None,
                halign="center", valign="center", wrap=False, border=None, italic=False):
        c = ws.cell(row=row, column=col, value=val)
        c.font      = Font(bold=bold, size=size, color=color, italic=italic, name="Calibri")
        c.alignment = Alignment(horizontal=halign, vertical=valign, wrap_text=wrap)
        if bg:     c.fill   = PatternFill("solid", fgColor=bg)
        if border: c.border = border
        else:      c.border = bdr
        return c

    # Preload ALL attendance for this scope (one query)
    _scope_att_cache = {}
    def _preload_att(m_val, y_val):
        key = (m_val, y_val)
        if key not in _scope_att_cache:
            rows = conn.execute("""SELECT * FROM attendance
                WHERE strftime('%m',att_date)=? AND strftime('%Y',att_date)=?
                ORDER BY emp_code, att_date""",
                (f"{m_val:02d}", str(y_val))).fetchall()
            from collections import defaultdict as _ddd
            grp = _ddd(dict)
            byemp = _ddd(dict)
            for r in rows:
                byemp[r["emp_code"]][r["att_date"]] = dict(r)
            _scope_att_cache[key] = byemp
        return _scope_att_cache[key]

    def get_att_month(ec, m, y):
        byemp = _preload_att(m, y)
        return byemp.get(ec, {})

    def make_sheet_detailed(emp, m, y):
        """Detailed report like Format 1"""
        sname = f"{emp['emp_code']}"[:31]
        if sname in [s.title for s in wb.worksheets]:
            sname = sname + f"_{m}"
        ws = wb.create_sheet(title=sname)

        days_in_month = cal_mod.monthrange(y, m)[1]
        att = get_att_month(emp["emp_code"], m, y)

        # === HEADER ===
        total_cols = days_in_month + 2
        ws.merge_cells(f"A1:{get_column_letter(total_cols)}1")
        mk_cell(ws,1,1, f"VIJAYSHRI PACKAGING LTD. — Attendance Register {MONTHS[m-1]} {y}",
                bold=True, size=12, color="FFFFFF", bg=COLORS["header_dark"],
                border=bdr_t)
        ws.row_dimensions[1].height = 24

        # Employee info
        ws.merge_cells(f"A2:{get_column_letter(total_cols//2)}2")
        mk_cell(ws,2,1, f"Employee: {emp['emp_code']} : {emp['emp_name']}",
                bold=True, size=10, color="FFFFFF", bg=COLORS["row_label"], halign="left")
        ws.merge_cells(f"{get_column_letter(total_cols//2+1)}2:{get_column_letter(total_cols)}2")
        mk_cell(ws,2,total_cols//2+1, f"Department: {emp['department'] or '—'}  |  Category: {emp['category']}",
                bold=True, size=10, color="FFFFFF", bg=COLORS["row_label"], halign="left")
        ws.row_dimensions[2].height = 18

        # === ROW LABELS (col 1) ===
        row_labels = ["Days","Status","In Time","Out Time","Duration","Late By","Early By","OT","Shift"]
        row_colors = [COLORS["header_mid"]]*1 + [COLORS["row_label"]]*8
        for ri, (lbl, clr) in enumerate(zip(row_labels, row_colors), 3):
            mk_cell(ws,ri,1, lbl, bold=True, size=9, color="FFFFFF", bg=clr, halign="left")
            ws.row_dimensions[ri].height = 15

        # Col widths
        ws.column_dimensions["A"].width = 10
        ws.column_dimensions["B"].width = 4  # total col

        # === DATE COLUMNS ===
        for day in range(1, days_in_month+1):
            col = day + 1
            dt_str = f"{y}-{m:02d}-{day:02d}"
            day_obj = date(y, m, day)
            day_name = PY_TO_DISPLAY[day_obj.weekday()]
            rec = att.get(dt_str, {})
            status = rec.get("status","") or ""
            is_sun = day_obj.weekday() == get_emp_weekly_off_num(emp["emp_code"])

            # Day name color
            day_bg = COLORS["wo"] if is_sun else COLORS["header_light"]
            day_label = str(day) + chr(10) + day_name
            mk_cell(ws,3,col, day_label, bold=True, size=8,
                    color="0052CC" if not is_sun else "92400E",
                    bg=day_bg, wrap=True)
            ws.column_dimensions[get_column_letter(col)].width = 6
            ws.row_dimensions[3].height = 22

            # Status color
            is_hd_rec = rec.get("is_half_day", 0) or 0
            st_bg = {
                "Present":COLORS["present"], "P":COLORS["present"],
                "Absent":COLORS["absent"],   "A":COLORS["absent"],
                "WO":COLORS["wo"],           "WOP":COLORS["wop"],
                "Leave":COLORS["absent"],    "Half Day":"FFD9E8"
            }.get(status, COLORS["white"])
            if status in ("Present","P") and is_hd_rec:
                short_st = "P½"
                st_bg = "FFD9E8"
            else:
                short_st = {"Present":"P","Absent":"A","WO":"WO","WOP":"WOP","Leave":"L","Half Day":"P½"}.get(status, status[:2] if status else "")
            mk_cell(ws,4,col, short_st, bold=True, size=8,
                    color="8B0045" if short_st=="P½" else "166534" if "P" in short_st else "991B1B" if "A" in short_st else "854D0E",
                    bg=st_bg)

            # In/Out/Duration
            in_t  = rec.get("in_time","")  or ""
            out_t = rec.get("out_time","") or ""
            wm    = rec.get("working_minutes",0) or 0
            late  = rec.get("late_minutes",0) or 0
            short = rec.get("short_minutes",0) or 0
            ot    = rec.get("ot_minutes",0)   or 0
            shift_nm = rec.get("shift_name","") or ""

            # Duration = actual total working hours (IN to OUT)
            # OT is shown separately in OT row
            dur = f"{wm//60}:{wm%60:02d}" if wm > 0 else ""

            mk_cell(ws,5,col, in_t,  size=8, color="166534", bg=COLORS["white"])
            mk_cell(ws,6,col, out_t, size=8, color="0052CC", bg=COLORS["white"])
            mk_cell(ws,7,col, dur,   size=8, bg=COLORS["white"])
            late_str  = f"{late//60}:{late%60:02d}"   if late  else ""
            early_str = f"{short//60}:{short%60:02d}" if short else ""
            mk_cell(ws,8,col,  late_str,  size=8, color="DC2626", bg=COLORS["white"])
            mk_cell(ws,9,col,  early_str, size=8, color="F59E0B", bg=COLORS["white"])
            ot_str = f"{ot//60}:{ot%60:02d}" if ot else ""
            mk_cell(ws,10,col, ot_str,  size=8, color="7C3AED", bg=COLORS["white"])
            mk_cell(ws,11,col, shift_nm, size=7, color="0052CC", bg=COLORS["white"])

        # Summary col (last col)
        sc = days_in_month + 2
        ws.column_dimensions[get_column_letter(sc)].width = 10
        mk_cell(ws,3,sc, "Total", bold=True, size=9, color="FFFFFF", bg=COLORS["header_dark"])
        present = sum(1 for r in att.values() if r.get("status") in ("Present","P","WOP"))
        absent  = sum(1 for r in att.values() if r.get("status") in ("Absent","A"))
        wo      = sum(1 for r in att.values() if r.get("status") == "WO")
        wop     = sum(1 for r in att.values() if r.get("status") == "WOP")
        tot_ot  = sum(r.get("ot_minutes",0) or 0 for r in att.values())
        tot_late= sum(r.get("late_minutes",0) or 0 for r in att.values())

        # Shift count like eSSL: GS:12 NS:5
        from collections import Counter
        shift_counts = Counter(r.get("shift_name","") for r in att.values() if r.get("shift_name"))
        shift_str = " ".join(f"{k}:{v}" for k,v in shift_counts.most_common())

        mk_cell(ws,4,sc, f"P:{present}\nA:{absent}", size=8, wrap=True, bg=COLORS["alt_row"])
        mk_cell(ws,5,sc, "", bg=COLORS["white"])
        mk_cell(ws,6,sc, "", bg=COLORS["white"])
        mk_cell(ws,7,sc, "", bg=COLORS["white"])
        late_tot_str = f"{tot_late//60}:{tot_late%60:02d}" if tot_late else ""
        mk_cell(ws,8,sc, late_tot_str, size=8, bold=True, color="DC2626", bg=COLORS["alt_row"])
        mk_cell(ws,9,sc, "", bg=COLORS["white"])
        mk_cell(ws,10,sc, f"{tot_ot//60}:{tot_ot%60:02d}", size=8, bold=True,
                color="7C3AED", bg=COLORS["alt_row"])
        mk_cell(ws,11,sc, shift_str, size=7, bold=True,
                color="0052CC", bg=COLORS["alt_row"], wrap=True)

    def make_sheet_inout(emp, m, y):
        """In/Out only report like Format 2"""
        sname = f"IO_{emp['emp_code']}"[:31]
        ws = wb.create_sheet(title=sname)
        att = get_att_month(emp["emp_code"], m, y)

        # Filter: only days with records
        att_list = sorted(att.items())

        # Header
        ws.merge_cells("A1:G1")
        mk_cell(ws,1,1, f"VIJAYSHRI PACKAGING LTD. — In/Out Report {MONTHS[m-1]} {y}",
                bold=True, size=12, color="FFFFFF", bg=COLORS["header_dark"])
        ws.row_dimensions[1].height = 24

        ws.merge_cells("A2:D2")
        mk_cell(ws,2,1, f"Emp. Code: {emp['emp_code']}  |  Emp. Name: {emp['emp_name']}",
                bold=True, size=10, color="FFFFFF", bg=COLORS["row_label"], halign="left")
        ws.merge_cells("E2:G2")
        mk_cell(ws,2,5, f"Department: {emp['department'] or '—'}",
                bold=True, size=10, color="FFFFFF", bg=COLORS["row_label"], halign="left")
        ws.row_dimensions[2].height = 18

        # Column headers
        hdrs = ["Day","Date","Status","In Time","Out Time","Duration","OT"]
        for ci, h in enumerate(hdrs, 1):
            mk_cell(ws,3,ci, h, bold=True, size=9, color="FFFFFF", bg=COLORS["row_label"])
        ws.row_dimensions[3].height = 16
        for ci, w in enumerate([5,12,8,10,10,10,8], 1):
            ws.column_dimensions[get_column_letter(ci)].width = w

        # Data rows
        row = 4
        for dt_str, rec in att_list:
            if not rec.get("in_time"): continue
            day_obj = date.fromisoformat(dt_str)
            day_name = PY_TO_DISPLAY[day_obj.weekday()]
            status = rec.get("status","") or ""
            wm = rec.get("working_minutes",0) or 0
            ot = rec.get("ot_minutes",0) or 0
            bg = COLORS["present"] if "P" in status else COLORS["absent"] if "A" in status else COLORS["wo"]
            mk_cell(ws,row,1, day_name, size=9, bg=bg)
            mk_cell(ws,row,2, dt_str[8:] + "/" + dt_str[5:7], size=9, bg=bg)
            mk_cell(ws,row,3, status, bold=True, size=9, bg=bg)
            mk_cell(ws,row,4, rec.get("in_time","") or "—", size=9, color="166534", bg=COLORS["white"])
            mk_cell(ws,row,5, rec.get("out_time","") or "—", size=9, color="0052CC", bg=COLORS["white"])
            mk_cell(ws,row,6, f"{wm//60}:{wm%60:02d}" if wm else "—", size=9, bg=COLORS["white"])
            mk_cell(ws,row,7, f"{ot//60}:{ot%60:02d}" if ot else "—", size=9, color="7C3AED", bg=COLORS["white"])
            row += 1

        # Summary
        ws.merge_cells(f"A{row}:C{row}")
        tot_wm = sum(r.get("working_minutes",0) or 0 for r in att.values())
        tot_ot = sum(r.get("ot_minutes",0)      or 0 for r in att.values())
        present = sum(1 for r in att.values() if r.get("status") in ("Present","P","WOP"))
        mk_cell(ws,row,1, f"Total Present: {present}", bold=True, size=9,
                color="FFFFFF", bg=COLORS["header_dark"])
        mk_cell(ws,row,4, "", bg=COLORS["alt_row"])
        mk_cell(ws,row,5, "", bg=COLORS["alt_row"])
        mk_cell(ws,row,6, f"{tot_wm//60}:{tot_wm%60:02d}", bold=True, size=9,
                color="0052CC", bg=COLORS["alt_row"])
        mk_cell(ws,row,7, f"{tot_ot//60}:{tot_ot%60:02d}", bold=True, size=9,
                color="7C3AED", bg=COLORS["alt_row"])

    # Generate sheets for each employee
    months_to_gen = [month] if scope == "monthly" else list(range(1,13))

    for emp in emps:
        for m in months_to_gen:
            if rpt_type == "detailed":
                make_sheet_detailed(emp, m, year)
            elif rpt_type == "inout":
                make_sheet_inout(emp, m, year)
            else:
                make_sheet_detailed(emp, m, year)
                make_sheet_inout(emp, m, year)

    conn.close()

    if not wb.worksheets:
        ws = wb.create_sheet("No Data")
        ws["A1"] = "No attendance data found for selected filters"
        
    scope_str = MONTHS[month-1] if scope == "monthly" else str(year)
    emp_str   = emp_code or (dept or "All")
    fname = f"Attendance_{rpt_type}_{emp_str}_{scope_str}.xlsx"
    return xlresp(wb, fname)



@app.route("/attendance/fix-weekly-off", methods=["POST"])
@amgr
def fix_weekly_off():
    """
    Fix Weekly Off records for all employees based on their weekly_off setting.
    For each employee:
    - Any day matching their weekly_off day with no punch → set status=WO
    - Any day matching their weekly_off day WITH punch → set status=WOP
    - Any WO record on a day that is NOT their weekly_off → set status=Absent (if no punch)
    """
    import calendar as _cal
    from datetime import date as _dt, timedelta as _td

    d = request.json or {}
    month = int(d.get("month", date.today().month))
    year  = int(d.get("year",  date.today().year))

    conn = get_db()
    try:
        emps = conn.execute("SELECT emp_code, category, weekly_off FROM employees WHERE status='Active'").fetchall()
        hols = {h["holiday_date"] for h in conn.execute(
            """SELECT holiday_date FROM holidays
               WHERE strftime('%m',holiday_date)=? AND strftime('%Y',holiday_date)=?""",
            (f"{month:02d}", str(year))).fetchall()}

        total_days = _cal.monthrange(year, month)[1]
        fixed = 0

        for emp in emps:
            ec = emp["emp_code"]
            wo_num = get_emp_weekly_off_num(ec, conn)

            for day in range(1, total_days + 1):
                d_obj  = _dt(year, month, day)
                dt_str = d_obj.strftime("%Y-%m-%d")
                is_wo_day = (d_obj.weekday() == wo_num)
                is_hol    = dt_str in hols

                rec = conn.execute(
                    "SELECT in_time, out_time, status FROM attendance WHERE emp_code=? AND att_date=?",
                    (ec, dt_str)).fetchone()

                has_punch = rec and rec["in_time"] and rec["in_time"].strip()

                if is_wo_day and not is_hol:
                    if has_punch:
                        # Punched on WO day → WOP
                        if rec["status"] != "WOP":
                            conn.execute(
                                "UPDATE attendance SET status='WOP' WHERE emp_code=? AND att_date=?",
                                (ec, dt_str))
                            fixed += 1
                    else:
                        # No punch on WO day → WO
                        if rec:
                            if rec["status"] != "WO":
                                conn.execute(
                                    """UPDATE attendance SET status='WO', working_minutes=0,
                                       ot_minutes=0, late_minutes=0, short_minutes=0
                                       WHERE emp_code=? AND att_date=?""",
                                    (ec, dt_str))
                                fixed += 1
                        else:
                            conn.execute(
                                """INSERT OR IGNORE INTO attendance
                                   (emp_code,att_date,in_time,out_time,working_minutes,status,
                                    late_minutes,short_minutes,ot_minutes,is_half_day)
                                   VALUES (?,?,'','',0,'WO',0,0,0,0)""",
                                (ec, dt_str))
                            fixed += 1
                elif not is_wo_day and not is_hol:
                    # Not a WO day — if marked WO wrongly (no punch) → Absent
                    if rec and rec["status"] == "WO" and not has_punch:
                        conn.execute(
                            "UPDATE attendance SET status='Absent' WHERE emp_code=? AND att_date=?",
                            (ec, dt_str))
                        fixed += 1

        conn.commit()
        return jsonify({"success": True, "fixed": fixed,
                        "message": f"Fixed {fixed} attendance records for {MONTHS[month-1]} {year}"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})
    finally:
        conn.close()

@app.route("/attendance/fix-miss-punch-today", methods=["POST"])
@amgr
def fix_miss_punch_today():
    """
    Fix Miss Punch records for today where shift has NOT ended yet.
    These were incorrectly saved as Miss Punch — should be Present.
    Called automatically on attendance page load OR manually via button.
    """
    from datetime import datetime as _dtnow_fix
    conn = get_db()
    today_str = date.today().strftime("%Y-%m-%d")
    now_min   = _dtnow_fix.now().hour * 60 + _dtnow_fix.now().minute
    fixed     = 0

    # Get all Miss Punch records for today that have an IN time
    mp_rows = conn.execute("""
        SELECT a.emp_code, a.att_date, a.in_time, a.out_time
        FROM attendance a
        WHERE a.att_date = ? AND a.status = 'Miss Punch'
        AND a.in_time IS NOT NULL AND a.in_time != ''
        AND (a.out_time IS NULL OR a.out_time = '')
    """, (today_str,)).fetchall()

    for row in mp_rows:
        ec = row["emp_code"]
        # Get shift end time from roster
        sh = conn.execute("""SELECT s.end_time, s.is_night_shift
            FROM shifts s JOIN shift_roster_dates srd ON s.id=srd.shift_id
            WHERE srd.emp_code=? AND srd.shift_date=?""",
            (ec, today_str)).fetchone()

        shift_ended = False
        if sh and sh["end_time"]:
            try:
                eh, em = map(int, sh["end_time"].split(":")[:2])
                end_m  = eh*60+em
                if sh["is_night_shift"] and end_m < 12*60:
                    end_m += 24*60
                if now_min > end_m + 30:
                    shift_ended = True
            except:
                shift_ended = True
        else:
            # No shift — if IN was > 9 hrs ago
            try:
                ih, im = map(int, str(row["in_time"]).split(":")[:2])
                if now_min > ih*60+im+9*60:
                    shift_ended = True
            except:
                shift_ended = True

        if not shift_ended:
            # Shift still ongoing — fix to Present
            conn.execute("""UPDATE attendance SET status='Present'
                WHERE emp_code=? AND att_date=? AND status='Miss Punch'""",
                (ec, today_str))
            fixed += 1

    conn.commit()
    conn.close()
    return jsonify({"success": True, "fixed": fixed,
                    "message": f"Fixed {fixed} Miss Punch records to Present."})


@app.route("/attendance/recalculate", methods=["POST"])
@amgr
def recalculate_attendance():
    """
    Recalculate attendance using eTimeTrack pairing logic.
    Re-reads in_time/out_time from DB and recalculates with correct shift.
    Also supports date range mode.
    """
    d        = request.json
    emp_code = str(d.get("emp_code","") or "").strip()
    from_date = str(d.get("from_date","") or "").strip()
    to_date   = str(d.get("to_date","")   or "").strip()

    conn = get_db()

    if from_date and to_date:
        date_sql  = "att_date BETWEEN ? AND ?"
        date_vals = [from_date, to_date]
        label = f"{from_date} to {to_date}"
    else:
        month = int(d.get("month", date.today().month))
        year  = int(d.get("year",  date.today().year))
        date_sql  = "strftime('%m',att_date)=? AND strftime('%Y',att_date)=?"
        date_vals = [f"{month:02d}", str(year)]
        label = f"{MONTHS[month-1]} {year}"

    emp_sql = "SELECT * FROM employees WHERE status='Active'"
    ep = []
    if emp_code: emp_sql += " AND emp_code=?"; ep.append(emp_code)
    emps = conn.execute(emp_sql, ep).fetchall()

    updated = 0

    # ── Load Roster Map for ALL employees in range ────────────────────────
    # This is needed for Shift Window Based Pairing in etimetrack_pair_punches
    if from_date and to_date:
        _r_from, _r_to = from_date, to_date
    else:
        import calendar as _cal
        _m2 = int(d.get("month", date.today().month))
        _y2 = int(d.get("year",  date.today().year))
        _r_from = f"{_y2}-{_m2:02d}-01"
        _r_to   = f"{_y2}-{_m2:02d}-{_cal.monthrange(_y2,_m2)[1]:02d}"

    _roster_rows = conn.execute("""SELECT srd.emp_code, srd.shift_date, s.*
        FROM shift_roster_dates srd JOIN shifts s ON srd.shift_id=s.id
        WHERE srd.shift_date BETWEEN ? AND ?""",
        (_r_from, _r_to)).fetchall()
    full_roster_map = {(r["emp_code"], r["shift_date"]): dict(r) for r in _roster_rows}

    for emp in emps:
        ec  = emp["emp_code"]
        cat = emp["category"]

        use_shifts = []  # Not used for auto-detect; window pairing uses roster_map

        # Per-employee roster map for window-based pairing
        emp_roster_map = {k: v for k, v in full_roster_map.items()
                          if isinstance(k, tuple) and k[0] == ec}

        # Fetch attendance rows for this period
        rows = conn.execute(f"""SELECT * FROM attendance
            WHERE emp_code=? AND {date_sql}
            ORDER BY att_date, in_time""",
            [ec] + date_vals).fetchall()
        if not rows: continue

        # Reconstruct datetime objects from stored in_time/out_time + att_date
        # Then re-run etimetrack pairing on them
        from datetime import datetime as _dtr, timedelta as _tdr
        punch_dts = []

        # Track manual dates to skip in final save
        manual_dates = set()
        for row in rows:
            if row["is_manual"] == 1:
                manual_dates.add(row["att_date"])  # These rows are untouchable

        for row in rows:
            att_d  = row["att_date"]

            # Skip manual entries — they are directly in DB and must not be re-paired
            if row["is_manual"] == 1:
                continue
            has_in  = bool(row["in_time"] and str(row["in_time"]).strip())
            has_out = bool(row["out_time"] and str(row["out_time"]).strip())

            # ── Staff special rule ──────────────────────────────
            # If ONLY OUT exists (no IN on same date) → orphan punch
            # Staff: this means IN was missed. Mark that day as Absent.
            # Do NOT add this orphan punch to the stream — it would cause
            # wrong pairing (treated as night-shift IN → next day's OUT becomes its pair)
            if cat == "Staff" and has_out and not has_in:
                # Mark this day explicitly as Absent (missing IN punch)
                try:
                    conn.execute("""UPDATE attendance
                        SET status='Absent', working_minutes=0, ot_minutes=0,
                            late_minutes=0, short_minutes=0, is_half_day=0,
                            remarks='IN punch missing — marked Absent on recalculate'
                        WHERE emp_code=? AND att_date=?""", (ec, att_d))
                except: pass
                # Orphan OUT not added to punch stream — avoids wrong pairing
                continue

            if has_in:
                try:
                    in_dt_candidate = _dtr.strptime(f"{att_d} {row['in_time']}", "%Y-%m-%d %H:%M")
                    # Cross-midnight guard: if previous day's OUT punch crosses into this day
                    # and equals this IN time → this IN is a duplicate of the cross-midnight OUT
                    # Skip adding it to avoid double-counting
                    _skip_in = False
                    if punch_dts:
                        last_punch = punch_dts[-1]
                        if last_punch == in_dt_candidate:
                            _skip_in = True  # Already in stream as OUT of previous day
                        prev_date_str = (_dtr.strptime(att_d, "%Y-%m-%d") - _tdr(days=1)).strftime("%Y-%m-%d")
                        prev_row_chk = conn.execute(
                            "SELECT in_time, out_time FROM attendance WHERE emp_code=? AND att_date=?",
                            (ec, prev_date_str)).fetchone()
                        if prev_row_chk and prev_row_chk["in_time"] and prev_row_chk["out_time"]:
                            try:
                                _prev_in  = _dtr.strptime(f"{prev_date_str} {prev_row_chk['in_time']}", "%Y-%m-%d %H:%M")
                                _prev_out = _dtr.strptime(f"{prev_date_str} {prev_row_chk['out_time']}", "%Y-%m-%d %H:%M")
                                if _prev_out < _prev_in:  # cross-midnight shift
                                    _prev_out_next = _prev_out + _tdr(days=1)
                                    if _prev_out_next == in_dt_candidate:
                                        _skip_in = True  # This IN = prev day's cross-midnight OUT
                                        try:
                                            _att_dow2 = _dtr.strptime(att_d, "%Y-%m-%d").weekday()
                                            _wo_num2  = get_emp_weekly_off_num(ec, conn)
                                            _fix_status = "WO" if _att_dow2 == _wo_num2 else "Absent"
                                            conn.execute("""UPDATE attendance
                                                SET in_time=NULL, out_time=NULL,
                                                    status=?,
                                                    working_minutes=0, ot_minutes=0,
                                                    late_minutes=0, short_minutes=0,
                                                    remarks='Cross-midnight OUT cleared on recalculate'
                                                WHERE emp_code=? AND att_date=?""",
                                                (_fix_status, ec, att_d))
                                        except: pass
                            except: pass

                        # ── eSSL-style: prev day night shift, no OUT stored, but today IN = expected night OUT ──
                        # Scenario: 11 May IN=18:59, OUT=NULL; 12 May IN=06:15 (this is actually 11's night OUT)
                        if not _skip_in and prev_row_chk and prev_row_chk["in_time"] and not prev_row_chk["out_time"]:
                            try:
                                _prev_in2 = _dtr.strptime(f"{prev_date_str} {prev_row_chk['in_time']}", "%Y-%m-%d %H:%M")
                                # Check if prev IN is a night shift time (evening hours 17:00-23:59)
                                _prev_in2_min = _prev_in2.hour * 60 + _prev_in2.minute
                                _today_in_min = in_dt_candidate.hour * 60 + in_dt_candidate.minute
                                # Night shift heuristic: prev IN >= 17:00 AND today IN is 00:00-10:00
                                # AND today IN within 10 min of some existing punch (dedup scenario)
                                if _prev_in2_min >= 17*60 and _today_in_min <= 10*60:
                                    # This current day's IN is likely the cross-midnight OUT of prev night shift
                                    # Check roster: is prev day a night shift?
                                    _prev_shift = emp_roster_map.get((ec, prev_date_str)) or {}
                                    _is_ns = int(_prev_shift.get("is_night_shift", 0) or 0)
                                    if _is_ns or _prev_in2_min >= 17*60:  # night shift or late IN
                                        _skip_in = True
                                        try:
                                            # Clear the orphan IN from today's record
                                            # Also set status=WO if today is employee's weekly off
                                            _att_dow = _dtr.strptime(att_d, "%Y-%m-%d").weekday()
                                            _wo_num_chk = get_emp_weekly_off_num(ec, conn)
                                            _correct_status = "WO" if _att_dow == _wo_num_chk else "Absent"
                                            conn.execute("""UPDATE attendance
                                                SET in_time=NULL, out_time=NULL,
                                                    status=?,
                                                    working_minutes=0, ot_minutes=0,
                                                    late_minutes=0, short_minutes=0,
                                                    remarks='Night shift orphan IN cleared — cross-midnight OUT'
                                                WHERE emp_code=? AND att_date=?""",
                                                (_correct_status, ec, att_d))
                                        except: pass
                            except: pass
                    if not _skip_in:
                        punch_dts.append(in_dt_candidate)
                except: pass
            if has_out:
                try:
                    out_dt = _dtr.strptime(f"{att_d} {row['out_time']}", "%Y-%m-%d %H:%M")
                    # If OUT < IN on same date, it's next day (cross-midnight night shift)
                    if has_in:
                        in_dt = _dtr.strptime(f"{att_d} {row['in_time']}", "%Y-%m-%d %H:%M")
                        if out_dt < in_dt:
                            out_dt = out_dt + _tdr(days=1)
                    punch_dts.append(out_dt)
                except: pass

            # ── Cross-month night shift guard ─────────────────────────────
            # If this record has NO in_time but HAS out_time, AND previous day
            # was a night shift → this out_time is carry-over from previous day.
            # Do NOT add it as IN punch for this date.
            if not has_in and has_out:
                try:
                    prev_date = (_dtr.strptime(att_d, "%Y-%m-%d") - _tdr(days=1)).strftime("%Y-%m-%d")
                    prev_row = conn.execute(
                        "SELECT in_time, out_time FROM attendance WHERE emp_code=? AND att_date=?",
                        (ec, prev_date)).fetchone()
                    # Check if previous day had IN but no OUT (meaning OUT came next day)
                    prev_shift = conn.execute("""SELECT s.is_night_shift, s.end_time FROM shifts s
                        JOIN shift_roster_dates srd ON s.id=srd.shift_id
                        WHERE srd.emp_code=? AND srd.shift_date=?""",
                        (ec, prev_date)).fetchone()
                    if prev_shift and (prev_shift["is_night_shift"] or
                       (prev_shift["end_time"] and int(prev_shift["end_time"][:2]) < 12)):
                        # Previous day was night shift — this out_time belongs to prev day
                        # Already added above as out_dt with +1 day. Remove it from today's IN.
                        out_dt_crossmonth = _dtr.strptime(f"{att_d} {row['out_time']}", "%Y-%m-%d %H:%M")
                        if out_dt_crossmonth in punch_dts:
                            punch_dts.remove(out_dt_crossmonth)
                except: pass

        if not punch_dts: continue

        # Sort and re-pair — Shift Window Based Pairing with roster
        punch_dts = sorted(set(punch_dts))
        pairs = etimetrack_pair_punches(punch_dts, use_shifts, category=cat,
                                        roster_map=emp_roster_map)

        for pair in pairs:
            att_date = pair["date"]
            # Check date is in requested range
            if from_date and to_date:
                if not (from_date <= att_date <= to_date): continue
            elif not (date_vals[0] == f"{int(att_date[5:7]):02d}" and date_vals[1] == att_date[:4]):
                continue

            # Skip manual entry dates — already saved correctly in DB
            if att_date in manual_dates:
                continue

            # Check roster override
            roster_row = conn.execute("""SELECT s.* FROM shifts s
                JOIN shift_roster_dates srd ON s.id=srd.shift_id
                WHERE srd.emp_code=? AND srd.shift_date=? AND s.is_active=1""",
                (ec, att_date)).fetchone()
            if roster_row:
                pair["shift"] = dict(roster_row)

            try:
                from datetime import date as _dtc
                att_dt = _dtc.fromisoformat(att_date)
                # Check if existing record has Leave/Manual status — preserve it
                existing = conn.execute(
                    "SELECT status, remarks, is_manual FROM attendance WHERE emp_code=? AND att_date=?",
                    (ec, att_date)).fetchone()
                preserved_status = None
                skip_entirely = False
                if existing:
                    ex_st = existing["status"] or ""
                    ex_rm = existing["remarks"] or ""
                    ex_manual = existing["is_manual"] or 0
                    if ex_manual == 1:
                        skip_entirely = True  # Manual entry — fully protected
                    elif "Cross-midnight OUT cleared" in ex_rm or "Night shift orphan IN cleared" in ex_rm:
                        skip_entirely = True  # Already fixed — don't override with Present
                    elif ex_st == "Absent" and (ex_rm or "").startswith("Cross-midnight") or                          ex_st == "Absent" and (ex_rm or "").startswith("Night shift"):
                        skip_entirely = True  # Already corrected to Absent — preserve
                    elif ex_st == "Leave" or "Manual" in ex_rm or "Leave" in ex_rm:
                        preserved_status = ex_st

                if skip_entirely:
                    continue  # Skip — manual entry is locked

                if preserved_status:
                    auto_status = preserved_status
                    status_ovrd = None
                elif att_dt.weekday() == get_emp_weekly_off_num(ec, conn):
                    auto_status = "WOP"
                    status_ovrd = "WOP"
                else:
                    hol = conn.execute("""SELECT id FROM holidays WHERE holiday_date=?
                        AND (applies_to='All' OR applies_to IS NULL OR applies_to=''
                             OR applies_to=? OR applies_to='Staff' OR applies_to='Associate')""",
                        (att_date, cat)).fetchone()
                    if hol:
                        auto_status = "Holiday"
                        # If employee punched on holiday → HP (OT calculated with 30min break)
                        status_ovrd = "Holiday" if pair["in_time"] else None
                    else:
                        # ── Miss Punch check (no OUT, not WOP/Holiday) ─────────────
                        if pair["in_time"] and not pair["out_time"]:
                            _is_mp_rc = False
                            try:
                                from datetime import datetime as _dtnow_rc, date as _date_rc
                                _att_dt_rc = _date_rc.fromisoformat(att_date)
                                _today_rc  = _date_rc.today()
                                if _att_dt_rc < _today_rc:
                                    # Past date — shift is over → always Miss Punch
                                    _is_mp_rc = True
                                elif _att_dt_rc == _today_rc:
                                    # Today — check if shift end + 30 min passed
                                    _now_rc = _dtnow_rc.now().hour * 60 + _dtnow_rc.now().minute
                                    _sh_rc = conn.execute("""SELECT s.end_time, s.is_night_shift
                                        FROM shifts s JOIN shift_roster_dates srd ON s.id=srd.shift_id
                                        WHERE srd.emp_code=? AND srd.shift_date=?""",
                                        (ec, att_date)).fetchone()
                                    if _sh_rc and _sh_rc["end_time"]:
                                        _eh_rc, _em_rc = map(int, _sh_rc["end_time"].split(":")[:2])
                                        _end_rc = _eh_rc*60+_em_rc
                                        if _sh_rc["is_night_shift"] and _end_rc < 12*60:
                                            _end_rc += 24*60
                                        if _now_rc > _end_rc + 30:
                                            _is_mp_rc = True
                                    else:
                                        # No shift — if 9+ hrs since IN
                                        _pi_rc = pair["in_time"] or ""
                                        _ih_rc, _im_rc = map(int, _pi_rc.split(":")[:2])
                                        if _now_rc > _ih_rc*60+_im_rc+9*60:
                                            _is_mp_rc = True
                                # Future date → not Miss Punch yet
                            except:
                                _is_mp_rc = True
                            auto_status = "Miss Punch" if _is_mp_rc else "Present"
                        else:
                            auto_status = "Present"
                        status_ovrd = None
            except:
                auto_status = "Present"
                status_ovrd = None

            save_att_row(conn, ec, att_date, pair["in_time"], pair["out_time"], cat,
                        status=auto_status,
                        status_override=status_ovrd if pair["in_time"] else None)
            updated += 1

        if updated % 100 == 0: conn.commit()

    # After pairing: insert Absent records for working days with no punch
    import calendar as _cal2
    from datetime import date as _dt2, timedelta as _td2

    for emp in emps:
        ec  = emp["emp_code"]
        cat = emp["category"]

        # Get date range to process
        if from_date and to_date:
            fd = _dt2.fromisoformat(from_date)
            td = _dt2.fromisoformat(to_date)
        else:
            fd = _dt2(year, month, 1)
            td = _dt2(year, month, _cal2.monthrange(year, month)[1])

        # Get holidays for this range
        hols_set = set()
        for h in conn.execute("""SELECT holiday_date FROM holidays
            WHERE holiday_date BETWEEN ? AND ?""", (fd.strftime("%Y-%m-%d"), td.strftime("%Y-%m-%d"))).fetchall():
            hols_set.add(h["holiday_date"])

        cur = fd
        while cur <= td:
            dt_str = cur.strftime("%Y-%m-%d")
            is_sun = (cur.weekday() == get_emp_weekly_off_num(ec))

            existing = conn.execute(
                "SELECT status FROM attendance WHERE emp_code=? AND att_date=?",
                (ec, dt_str)).fetchone()

            if is_sun:
                # Weekly off day — insert WO if no record yet (or if wrongly Absent)
                if not existing:
                    conn.execute("""INSERT OR IGNORE INTO attendance
                        (emp_code,att_date,in_time,out_time,working_minutes,status,
                         late_minutes,short_minutes,ot_minutes,is_half_day)
                        VALUES (?,?,'','',0,'WO',0,0,0,0)""",
                        (ec, dt_str))
                    updated += 1
                elif existing["status"] == "Absent":
                    # Fix wrongly-marked Absent on WO day (no punch = WO not Absent)
                    conn.execute("UPDATE attendance SET status='WO' WHERE emp_code=? AND att_date=?",
                        (ec, dt_str))
                    updated += 1
            elif dt_str not in hols_set:
                # ══════════════════════════════════════════════════════════
                # NIGHT SHIFT GUARD — Complete Logic
                # ══════════════════════════════════════════════════════════
                # Night Shift employee ke 3 cases:
                #
                # CASE 1: Future/Today ki night shift — punch abhi aayi nahi
                #   → Absent mat likho, skip karo
                #
                # CASE 2: Past night shift — punch aa chuki hogi
                #   → Agar record nahi hai → Absent likho (genuinely absent tha)
                #   → Agar record hai (Present/Miss Punch) → chhodo as-is
                #
                # CASE 3: Already galat Absent save tha (pehle recalculate se)
                #   → Night shift assigned hai + Absent record hai + koi punch nahi
                #   → Absent record DELETE karo (punch aane pe save hoga)
                # ══════════════════════════════════════════════════════════

                # Step A: Roster se Night Shift check karo
                is_night_roster_day = False
                night_shift_start_m = None
                try:
                    ns_row = conn.execute("""SELECT s.is_night_shift, s.start_time
                        FROM shifts s
                        JOIN shift_roster_dates srd ON s.id = srd.shift_id
                        WHERE srd.emp_code=? AND srd.shift_date=?""",
                        (ec, dt_str)).fetchone()
                    if ns_row:
                        _st_m = t2m_safe(ns_row["start_time"] or "")
                        night_shift_start_m = _st_m
                        is_night_roster_day = (
                            int(ns_row["is_night_shift"] or 0) == 1
                            or (_st_m is not None and _st_m >= 14 * 60)
                        )
                except: pass

                if is_night_roster_day:
                    # Step B: Aaj ya future ki date hai?
                    # Night shift start time se pehle recalculate chala → punch nahi aayi yet
                    # Shift start: e.g. 20:00 → 1200 min
                    from datetime import datetime as _dtnow
                    today_dt = _dt2.today()
                    now_min  = _dtnow.now().hour * 60 + _dtnow.now().minute

                    # Is date ki night shift abhi shuru nahi hui kya?
                    shift_not_started_yet = False
                    if cur >= today_dt:
                        # Aaj ya future date
                        if cur > today_dt:
                            shift_not_started_yet = True  # future date — definitely not started
                        else:
                            # Aaj ki date — shift start time check karo
                            if night_shift_start_m is not None:
                                shift_not_started_yet = (now_min < night_shift_start_m)
                            else:
                                shift_not_started_yet = (now_min < 20 * 60)  # default 20:00

                    if shift_not_started_yet:
                        # Punch ayi hi nahi — Absent mat likho, skip karo
                        # Agar pehle galat Absent save hua tha → woh bhi hatao
                        if existing and existing["status"] == "Absent":
                            conn.execute(
                                "DELETE FROM attendance WHERE emp_code=? AND att_date=?",
                                (ec, dt_str))
                            updated += 1
                        # else: koi record nahi → theek hai, kuch mat karo
                    else:
                        # Past night shift — punch aa chuki hogi ya genuinely absent
                        if not existing:
                            # Koi record nahi → Absent (genuinely nahi aaya tha)
                            conn.execute("""INSERT OR IGNORE INTO attendance
                                (emp_code,att_date,in_time,out_time,working_minutes,status,
                                 late_minutes,short_minutes,ot_minutes,is_half_day)
                                VALUES (?,?,'','',0,'Absent',0,0,0,0)""",
                                (ec, dt_str))
                            updated += 1
                        # else: record hai (Present/Miss Punch) → chhodo as-is

                else:
                    # Normal day shift employee
                    if not existing:
                        # Working day, no punch → Absent
                        conn.execute("""INSERT OR IGNORE INTO attendance
                            (emp_code,att_date,in_time,out_time,working_minutes,status,
                             late_minutes,short_minutes,ot_minutes,is_half_day)
                            VALUES (?,?,'','',0,'Absent',0,0,0,0)""",
                            (ec, dt_str))
                        updated += 1
                    elif existing["status"] == "Absent":
                        pass  # Already Absent — theek hai
            cur += _td2(days=1)

    # ── Bulk cleanup: clear cross-midnight orphan IN punches ─────────────
    # Find all attendance records where in_time == previous day's OUT time
    # AND previous day's OUT < previous day's IN (cross-midnight night shift)
    # These are orphan IN records that should have no in_time
    try:
        from datetime import datetime as _dtor
        _orphan_rows = conn.execute("""
            SELECT a1.emp_code, a1.att_date, a1.in_time,
                   a2.in_time as prev_in, a2.out_time as prev_out
            FROM attendance a1
            JOIN attendance a2
              ON a2.emp_code = a1.emp_code
              AND a2.att_date = DATE(a1.att_date, '-1 day')
            WHERE a1.in_time IS NOT NULL AND a1.in_time != ''
              AND a2.out_time IS NOT NULL AND a2.out_time != ''
              AND a2.in_time IS NOT NULL AND a2.in_time != ''
              AND a1.in_time = a2.out_time
              AND a2.out_time < a2.in_time
        """).fetchall()
        for _or in _orphan_rows:
            # Verify it's genuinely cross-midnight (not just same time coincidence)
            try:
                _pi = _dtor.strptime(f"{_or['att_date'][:7]}-{int(_or['att_date'][8:])-1:02d} {_or['prev_in']}", "%Y-%m-%d %H:%M") if True else None
                # Simple check: prev out < prev in (string comparison works for HH:MM)
                if _or["prev_out"] < _or["prev_in"]:  # cross-midnight confirmed
                    conn.execute("""UPDATE attendance
                        SET in_time=NULL, working_minutes=0, ot_minutes=0,
                            late_minutes=0, short_minutes=0,
                            remarks='Cross-midnight orphan IN cleared'
                        WHERE emp_code=? AND att_date=? AND in_time=?""",
                        (_or["emp_code"], _or["att_date"], _or["in_time"]))
                    updated += 1
            except: pass
    except: pass

    conn.commit(); conn.close()
    return jsonify({"success": True, "updated": updated,
                   "message": f"✅ {updated} records recalculated for {label}"})


@app.route("/shift-roster")
@amgr
def shift_roster():
    conn = get_db()
    today_s = date.today().strftime("%Y-%m-%d")
    emps   = conn.execute("SELECT emp_code,emp_name,department,category FROM employees WHERE status='Active' ORDER BY department,emp_name").fetchall()
    shifts = conn.execute("SELECT * FROM shifts WHERE is_active=1 ORDER BY shift_name").fetchall()
    depts  = conn.execute("SELECT DISTINCT department FROM employees WHERE status='Active' AND department IS NOT NULL ORDER BY department").fetchall()

    # Missing Shift: employees with no current/future shift assignment
    missing_shift = []
    for e in emps:
        ec = e["emp_code"]
        # Check permanent (fixed) assignment
        has_fixed = conn.execute("SELECT 1 FROM employee_shifts WHERE emp_code=?", (ec,)).fetchone()
        if has_fixed:
            continue
        # Check roster covering today or future
        has_roster = conn.execute(
            "SELECT MAX(shift_date) as last_dt FROM shift_roster_dates WHERE emp_code=?", (ec,)).fetchone()
        last_dt = has_roster["last_dt"] if has_roster else None
        # If no roster OR last roster date < today → missing
        if not last_dt or last_dt < today_s:
            missing_shift.append({
                "emp_code":  ec,
                "emp_name":  e["emp_name"],
                "department": e["department"] or "—",
                "category":  e["category"],
                "last_shift_date": last_dt or "Never",
                "reason": f"Expired after {last_dt}" if last_dt else "Never assigned"
            })

    conn.close()
    return render_template("shift_roster.html",
        employees=emps, shifts=shifts,
        departments=[d["department"] for d in depts],
        today=today_s,
        missing_shift=missing_shift)

@app.route("/shift-roster/assign-single", methods=["POST"])
@amgr
def shift_roster_assign_single():
    """Single employee — assign shift for date range"""
    d = request.json
    emp_code   = d.get("emp_code","").strip()
    shift_code = d.get("shift_code","").strip()
    from_date  = d.get("from_date","").strip()
    to_date    = d.get("to_date","").strip()
    if not emp_code or not shift_code or not from_date or not to_date:
        return jsonify({"success":False,"error":"All fields required"})
    conn = get_db()
    try:
        shift = conn.execute("SELECT * FROM shifts WHERE shift_code=? OR shift_name=?",
                             (shift_code, shift_code)).fetchone()
        if not shift:
            return jsonify({"success":False,"error":f"Shift '{shift_code}' not found"})
        # Save to employee_shifts with effective_from = from_date
        # Also save date-wise in shift_roster_dates table
        # INSERT OR REPLACE — nayi assignment purani ko override karegi
        # Step 1: Delete existing for this emp + date range
        conn.execute("""DELETE FROM shift_roster_dates
            WHERE emp_code=?
            AND shift_date BETWEEN ? AND ?""",
            (emp_code, from_date, to_date))
        # Step 2: Insert fresh for all dates in range
        conn.execute("""INSERT INTO shift_roster_dates (emp_code, shift_id, shift_date, assigned_by, assigned_on)
            SELECT ?, ?, date(?, '+'||seq||' days'), ?, datetime('now')
            FROM (WITH RECURSIVE cnt(seq) AS (
                SELECT 0 UNION ALL SELECT seq+1 FROM cnt
                WHERE seq < CAST((julianday(?) - julianday(?)) AS INTEGER)
            ) SELECT seq FROM cnt)""",
            (emp_code, shift["id"], from_date,
             session.get("name","HR"), to_date, from_date))
        # Also update employee_shifts (current active shift)
        conn.execute("""INSERT INTO employee_shifts (emp_code,shift_id,effective_from,assigned_by,assigned_on)
            VALUES (?,?,?,?,datetime('now'))
            ON CONFLICT(emp_code) DO UPDATE SET
            shift_id=excluded.shift_id,
            effective_from=excluded.effective_from,
            assigned_by=excluded.assigned_by,
            assigned_on=excluded.assigned_on""",
            (emp_code, shift["id"], from_date, session.get("name","HR")))
        conn.commit()
        # Count days assigned
        from_dt = datetime.strptime(from_date, "%Y-%m-%d").date()
        to_dt   = datetime.strptime(to_date,   "%Y-%m-%d").date()
        days    = (to_dt - from_dt).days + 1
        return jsonify({"success":True, "days":days, "shift":shift["shift_name"]})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})
    finally:
        conn.close()

@app.route("/shift-roster/assign-bulk", methods=["POST"])
@amgr
def shift_roster_assign_bulk():
    """Bulk Excel import — supports both formats:
       Format 1: emp_code, shift_code, date          (per-day)
       Format 2: emp_code, shift_code, from, to      (date range)
    """
    if "file" not in request.files:
        return jsonify({"success":False,"error":"No file uploaded"})
    try:
        import openpyxl
        from datetime import date as _dt, timedelta as _td
        wb = openpyxl.load_workbook(io.BytesIO(request.files["file"].read()))
        ws = wb.active
        # Auto-detect headers — normalize
        hdrs = []
        for cell in ws[1]:
            if cell.value:
                h = str(cell.value).strip().lower()
                h = h.replace(" ","_").replace("-","_").replace(".","")
                hdrs.append(h)
            else:
                hdrs.append("")

        conn = get_db()
        assigned = 0; errors = []

        def parse_date(val):
            """Parse date from Excel cell — handles datetime obj, string YYYY-MM-DD, DD-MM-YYYY"""
            if not val: return None
            if hasattr(val, "strftime"):
                return val.strftime("%Y-%m-%d")
            s = str(val).strip()
            for fmt in ["%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"]:
                try:
                    return datetime.strptime(s[:10], fmt).strftime("%Y-%m-%d")
                except: pass
            return None

        for row in ws.iter_rows(min_row=2, values_only=True):
            if not any(row): continue
            d = dict(zip(hdrs, row))

            emp_code   = str(d.get("emp_code","") or d.get("employee_code","") or "").strip()
            shift_code = str(d.get("shift_code","") or d.get("shift","") or "").strip()
            if not emp_code or not shift_code: continue

            # Detect format: range (from/to) or single date
            raw_from = d.get("from","") or d.get("from_date","") or d.get("start_date","") or ""
            raw_to   = d.get("to","")   or d.get("to_date","")   or d.get("end_date","")   or ""
            raw_date = d.get("date","") or d.get("shift_date","") or ""

            if raw_from and raw_to:
                # Format 2: date range
                from_date = parse_date(raw_from)
                to_date   = parse_date(raw_to)
                if not from_date or not to_date:
                    errors.append(f"{emp_code}: invalid date range {raw_from} - {raw_to}"); continue
                # Generate all dates in range
                dates = []
                fd = datetime.strptime(from_date, "%Y-%m-%d").date()
                td = datetime.strptime(to_date,   "%Y-%m-%d").date()
                cur = fd
                while cur <= td:
                    dates.append(cur.strftime("%Y-%m-%d"))
                    cur += _td(days=1)
            elif raw_date:
                # Format 1: single date
                att_date = parse_date(raw_date)
                if not att_date:
                    errors.append(f"Invalid date: {raw_date}"); continue
                dates = [att_date]
            else:
                continue

            # Validate shift
            shift = conn.execute("SELECT * FROM shifts WHERE shift_code=? OR shift_name=?",
                                (shift_code, shift_code)).fetchone()
            if not shift:
                errors.append(f"{emp_code}: shift '{shift_code}' not found"); continue

            # Validate employee
            emp = conn.execute("SELECT emp_code FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
            if not emp:
                # Try with leading zeros
                ec2 = emp_code.zfill(4)
                emp = conn.execute("SELECT emp_code FROM employees WHERE emp_code=?", (ec2,)).fetchone()
                if emp: emp_code = ec2
            if not emp:
                errors.append(f"Employee '{emp_code}' not found"); continue

            # Insert all dates
            for att_date in dates:
                conn.execute("""INSERT OR REPLACE INTO shift_roster_dates
                    (emp_code, shift_id, shift_date, assigned_by, assigned_on)
                    VALUES (?,?,?,?,datetime('now'))""",
                    (emp_code, shift["id"], att_date, session.get("name","HR")))
                assigned += 1

            # Update employee's active shift (use first date as effective_from)
            conn.execute("""INSERT INTO employee_shifts (emp_code,shift_id,effective_from,assigned_by,assigned_on)
                VALUES (?,?,?,?,datetime('now'))
                ON CONFLICT(emp_code) DO UPDATE SET
                shift_id=excluded.shift_id, effective_from=excluded.effective_from,
                assigned_by=excluded.assigned_by, assigned_on=excluded.assigned_on""",
                (emp_code, shift["id"], dates[0], session.get("name","HR")))

        conn.commit(); conn.close()
        return jsonify({"success":True,"assigned":assigned,"errors":errors[:10]})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})

@app.route("/shift-roster/export-report")
@amgr
def shift_roster_export_report():
    """Export shift roster assignments as Excel report — same format as upload template"""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter

    from_date = request.args.get("from_date", date.today().strftime("%Y-%m-%d"))
    to_date   = request.args.get("to_date",   date.today().strftime("%Y-%m-%d"))
    dept      = request.args.get("dept", "")

    conn = get_db()
    sql = """SELECT srd.shift_date, srd.emp_code, e.emp_name, e.department,
        e.category, s.shift_code, s.shift_name, s.start_time, s.end_time,
        srd.assigned_by, srd.assigned_on
        FROM shift_roster_dates srd
        JOIN employees e ON srd.emp_code=e.emp_code
        JOIN shifts s ON srd.shift_id=s.id
        WHERE srd.shift_date BETWEEN ? AND ?"""
    params = [from_date, to_date]
    if dept:
        sql += " AND e.department=?"
        params.append(dept)
    sql += " ORDER BY srd.shift_date, e.department, e.emp_code"
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Shift Roster Report"
    thin = Side(style="thin", color="D0DCF0")
    bdr  = Border(left=thin, right=thin, top=thin, bottom=thin)

    # Title
    ws.merge_cells("A1:I1")
    ws["A1"] = f"VIJAYSHRI PACKAGING LTD. — Shift Roster Report | {from_date} to {to_date}"
    ws["A1"].font = Font(bold=True, size=11, color="FFFFFF")
    ws["A1"].fill = PatternFill("solid", fgColor="0052CC")
    ws["A1"].alignment = Alignment(horizontal="center")

    # Headers — same columns as upload template PLUS extra info columns
    hdrs = ["emp_code","shift_code","from","to",
            "emp_name","department","category","shift_name","shift_timing"]
    hdr_notes = ["Employee Code","Shift Code","Date (YYYY-MM-DD)","Date (YYYY-MM-DD)",
                 "Employee Name","Department","Category","Shift Name","Timings"]
    for ci,(h,n) in enumerate(zip(hdrs,hdr_notes),1):
        cell = ws.cell(2, ci, h)
        cell.font = Font(bold=True, color="FFFFFF", size=10)
        cell.fill = PatternFill("solid", fgColor="1E3A5F")
        cell.alignment = Alignment(horizontal="center")
        cell.border = bdr
        nc = ws.cell(3, ci, n)
        nc.font = Font(italic=True, color="888888", size=9)
        nc.border = bdr

    widths = [12,12,16,16,24,18,12,20,16]
    for ci,w in enumerate(widths,1):
        ws.column_dimensions[get_column_letter(ci)].width = w

    # Data rows — exactly same format as upload template for cols A-D (uploadable)
    prev_date = None
    for ri,r in enumerate(rows, 4):
        bg = "F0F9FF" if ri % 2 == 0 else "FFFFFF"
        vals = [
            r["emp_code"], r["shift_code"], r["shift_date"], r["shift_date"],
            r["emp_name"], r["department"] or "—", r["category"],
            r["shift_name"], f"{r['start_time']}–{r['end_time']}"
        ]
        for ci,val in enumerate(vals,1):
            cell = ws.cell(ri, ci, val)
            cell.font = Font(size=9)
            cell.alignment = Alignment(horizontal="center")
            cell.border = bdr
            cell.fill = PatternFill("solid", fgColor=bg)
        # Highlight emp_code and shift_code (uploadable cols) in blue tint
        ws.cell(ri,1).font = Font(size=9, color="0052CC", bold=True)
        ws.cell(ri,2).font = Font(size=9, color="10B981", bold=True)

    # Footer note
    last_row = len(rows) + 5
    ws.merge_cells(f"A{last_row}:I{last_row}")
    ws[f"A{last_row}"] = "NOTE: Columns A (emp_code), B (shift_code), C (from), D (to) can be directly uploaded via Bulk Import Excel on Shift Roster page."
    ws[f"A{last_row}"].font = Font(italic=True, color="888888", size=9)

    out = io.BytesIO(); wb.save(out); out.seek(0)
    fname = f"ShiftRoster_{from_date}_to_{to_date}.xlsx"
    return send_file(out, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                     as_attachment=True, download_name=fname)


@app.route("/shift-roster/download-template")
@amgr
def shift_roster_template():
    """Download Excel template — shows both formats"""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    wb = openpyxl.Workbook()

    # Sheet 1: Range format (recommended)
    ws1 = wb.active
    ws1.title = "Range Format (Recommended)"
    hdrs1 = ["emp_code","shift_code","from","to"]
    notes = ["Employee Code","Shift Code (GS/NS etc.)","From Date (YYYY-MM-DD or DD-MM-YYYY)","To Date (YYYY-MM-DD or DD-MM-YYYY)"]
    for ci,(h,n) in enumerate(zip(hdrs1,notes),1):
        cell = ws1.cell(row=1, column=ci, value=h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="0052CC")
        cell.alignment = Alignment(horizontal="center")
        nc = ws1.cell(row=2, column=ci, value=n)
        nc.font = Font(italic=True, color="888888", size=9)
    samples1 = [
        ["1001","GS","01-03-2026","31-12-2026"],
        ["1002","GS","01-03-2026","31-12-2026"],
        ["1003","NS","01-03-2026","31-03-2026"],
    ]
    for ri, row in enumerate(samples1, 3):
        for ci, val in enumerate(row, 1):
            ws1.cell(row=ri, column=ci, value=val)
    for ci in range(1,5):
        ws1.column_dimensions[get_column_letter(ci)].width = 22

    # Sheet 2: Per-day format
    ws2 = wb.create_sheet("Per-Day Format")
    hdrs2 = ["emp_code","shift_code","date"]
    for ci, h in enumerate(hdrs2, 1):
        cell = ws2.cell(row=1, column=ci, value=h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="1E3A5F")
        cell.alignment = Alignment(horizontal="center")
    samples2 = [
        ["1001","GS","2026-03-01"],
        ["1001","NS","2026-03-02"],
        ["1002","GS","2026-03-01"],
    ]
    for ri, row in enumerate(samples2, 2):
        for ci, val in enumerate(row, 1):
            ws2.cell(row=ri, column=ci, value=val)
    for ci in range(1,4):
        ws2.column_dimensions[get_column_letter(ci)].width = 18

    out = io.BytesIO(); wb.save(out); out.seek(0)
    return send_file(out, mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    as_attachment=True, download_name="ShiftRoster_Template.xlsx")

@app.route("/shift-roster/get-assignments")
@amgr
def get_shift_roster_assignments():
    """Get shift assignments for a date range — for display"""
    from_date = request.args.get("from_date", date.today().strftime("%Y-%m-%d"))
    to_date   = request.args.get("to_date",   date.today().strftime("%Y-%m-%d"))
    dept      = request.args.get("dept","")
    conn = get_db()
    sql = """SELECT srd.emp_code, srd.shift_date, s.shift_name, s.shift_code,
                e.emp_name, e.department
             FROM shift_roster_dates srd
             JOIN shifts s ON srd.shift_id = s.id
             JOIN employees e ON srd.emp_code = e.emp_code
             WHERE srd.shift_date >= ? AND srd.shift_date <= ?"""
    params = [from_date, to_date]
    if dept:
        sql += " AND e.department=?"; params.append(dept)
    sql += " ORDER BY srd.shift_date, e.emp_name"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


# ─── MASTERS PAGE ────────────────────────────────────


# ─── EMPLOYEE CUSTOM FIELDS MASTER ─────────────────────────

@app.route("/masters/employee-fields")
@amgr
def emp_custom_fields_page():
    conn = get_db()
    fields = conn.execute("SELECT * FROM employee_custom_fields ORDER BY display_order,field_label").fetchall()
    conn.close()
    return jsonify({"success":True,"fields":[dict(f) for f in fields]})

@app.route("/masters/employee-fields/save", methods=["POST"])
@amgr
def emp_custom_fields_save():
    d = request.json
    conn = get_db()
    try:
        fid    = d.get("id")
        name   = str(d.get("field_name","")).strip().lower().replace(" ","_")
        label  = str(d.get("field_label","")).strip()
        ftype  = str(d.get("field_type","text")).strip()
        req    = 1 if d.get("is_required") else 0
        order  = int(d.get("display_order", 99))
        export = 1 if d.get("in_export", True) else 0
        if not name or not label:
            return jsonify({"success":False,"error":"Field name and label required"})
        options = str(d.get("options","") or "").strip()
        if fid:
            conn.execute("""UPDATE employee_custom_fields
                SET field_label=?,field_type=?,options=?,is_required=?,display_order=?,in_export=?
                WHERE id=?""", (label,ftype,options or None,req,order,export,fid))
        else:
            conn.execute("""INSERT INTO employee_custom_fields
                (field_name,field_label,field_type,options,is_required,display_order,in_export)
                VALUES (?,?,?,?,?,?,?)""", (name,label,ftype,options or None,req,order,export))
        conn.commit()
        return jsonify({"success":True})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})
    finally:
        conn.close()

@app.route("/masters/employee-fields/delete/<int:fid>", methods=["POST"])
@amgr
def emp_custom_fields_delete(fid):
    conn = get_db()
    try:
        field = conn.execute("SELECT field_name FROM employee_custom_fields WHERE id=?", (fid,)).fetchone()
        if field:
            conn.execute("DELETE FROM employee_custom_values WHERE field_name=?", (field["field_name"],))
        conn.execute("DELETE FROM employee_custom_fields WHERE id=?", (fid,))
        conn.commit()
        return jsonify({"success":True})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})
    finally:
        conn.close()

@app.route("/api/employee-custom-values/<emp_code>")
@amgr
def get_emp_custom_values(emp_code):
    conn = get_db()
    vals = conn.execute("SELECT field_name,field_value FROM employee_custom_values WHERE emp_code=?", (emp_code,)).fetchall()
    conn.close()
    return jsonify({r["field_name"]: r["field_value"] for r in vals})

@app.route("/api/employee-custom-values/<emp_code>/save", methods=["POST"])
@amgr
def save_emp_custom_values(emp_code):
    d = request.json or {}
    conn = get_db()
    try:
        for field_name, value in d.items():
            # Use INSERT OR REPLACE for compatibility
            conn.execute("""INSERT OR REPLACE INTO employee_custom_values (emp_code,field_name,field_value)
                VALUES (?,?,?)""",
                (emp_code, field_name, value))
        conn.commit()
        return jsonify({"success":True})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})
    finally:
        conn.close()

@app.route("/masters")
@amgr
def masters_page():
    conn = get_db()
    categories   = conn.execute("SELECT * FROM master_categories   ORDER BY name").fetchall()
    departments  = conn.execute("SELECT * FROM master_departments  ORDER BY name").fetchall()
    designations = conn.execute("SELECT * FROM master_designations ORDER BY name").fetchall()
    locations    = conn.execute("SELECT * FROM master_locations    ORDER BY name").fetchall()
    shifts_list  = conn.execute("SELECT * FROM shifts WHERE is_active=1 ORDER BY shift_name").fetchall()
    groups_list  = conn.execute("SELECT * FROM shift_groups WHERE is_active=1 ORDER BY group_name").fetchall()
    employees    = conn.execute("SELECT emp_code,emp_name,department,category FROM employees WHERE status='Active' ORDER BY emp_name").fetchall()
    # Employees with their assigned shift group
    assigned_groups = conn.execute("""SELECT esg.emp_code,sg.group_name FROM employee_shift_groups esg
        JOIN shift_groups sg ON esg.group_id=sg.id""").fetchall()
    assigned_map = {r["emp_code"]: r["group_name"] for r in assigned_groups}
    schemes = conn.execute("SELECT * FROM employee_schemes ORDER BY scheme_name").fetchall()
    conn.close()
    return render_template("masters.html",
        categories=categories, departments=departments,
        designations=designations, locations=locations,
        shifts=shifts_list, groups=groups_list,
        employees=employees, assigned_map=assigned_map,
        schemes=[dict(s) for s in schemes])

@app.route("/masters/<string:master_type>/add", methods=["POST"])
@amgr
def master_add(master_type):
    d = request.json
    name = str(d.get("name","")).strip()
    if not name: return jsonify({"success":False,"error":"Name required"})
    table_map = {
        "category":    "master_categories",
        "department":  "master_departments",
        "designation": "master_designations",
        "location":    "master_locations"
    }
    tbl = table_map.get(master_type)
    if not tbl: return jsonify({"success":False,"error":"Invalid type"})
    conn = get_db()
    try:
        conn.execute(f"INSERT INTO {tbl} (name,is_active) VALUES (?,1)", (name,))
        # Auto-add to dept_manpower when department created
        if master_type == "department":
            try:
                conn.execute("INSERT OR IGNORE INTO dept_manpower (department,std_staff,std_nonstaff) VALUES (?,0,0)", (name,))
                conn.execute("INSERT OR IGNORE INTO dept_ot_limits (department,monthly_ot_limit_hrs,per_day_hrs_per_emp) VALUES (?,50,2.5)", (name,))
            except: pass
        conn.commit()
        return jsonify({"success":True})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})
    finally: conn.close()

@app.route("/masters/<string:master_type>/edit/<int:mid>", methods=["POST"])
@amgr
def master_edit(master_type, mid):
    d = request.json
    name = str(d.get("name","")).strip()
    table_map = {"category":"master_categories","department":"master_departments","designation":"master_designations","location":"master_locations"}
    tbl = table_map.get(master_type)
    if not tbl: return jsonify({"success":False,"error":"Invalid type"})
    conn = get_db()
    try:
        # Get old name before update (for cascade sync)
        old_row = conn.execute(f"SELECT name FROM {tbl} WHERE id=?", (mid,)).fetchone()
        old_name = old_row["name"] if old_row else None

        conn.execute(f"UPDATE {tbl} SET name=?,is_active=? WHERE id=?",
                    (name, 1 if d.get("is_active",True) else 0, mid))

        # Cascade: if department name changed → update employees + related tables
        if master_type == "department" and old_name and old_name != name:
            conn.execute("UPDATE employees SET department=? WHERE department=?", (name, old_name))
            conn.execute("UPDATE attendance SET department=? WHERE department=?", (name, old_name)) if False else None
            try: conn.execute("UPDATE dept_manpower SET department=? WHERE department=?", (name, old_name))
            except: pass
            try: conn.execute("UPDATE dept_ot_limits SET department=? WHERE department=?", (name, old_name))
            except: pass

        conn.commit()
        return jsonify({"success":True})
    except Exception as e:
        return jsonify({"success":False,"error":str(e)})
    finally: conn.close()

@app.route("/masters/<string:master_type>/delete/<int:mid>", methods=["POST"])
@amgr
def master_delete(master_type, mid):
    table_map = {"category":"master_categories","department":"master_departments","designation":"master_designations","location":"master_locations"}
    tbl = table_map.get(master_type)
    if not tbl: return jsonify({"success":False,"error":"Invalid type"})
    conn = get_db()
    # Get name before deleting (for cascade removal)
    row = conn.execute(f"SELECT name FROM {tbl} WHERE id=?", (mid,)).fetchone()
    dept_name = row["name"] if row else None
    conn.execute(f"DELETE FROM {tbl} WHERE id=?", (mid,))
    # Cascade: remove from dept_manpower and dept_ot_limits when dept deleted
    if master_type == "department" and dept_name:
        try:
            conn.execute("DELETE FROM dept_manpower WHERE department=?", (dept_name,))
            conn.execute("DELETE FROM dept_ot_limits WHERE department=?", (dept_name,))
        except: pass
    conn.commit(); conn.close()
    return jsonify({"success":True})

@app.route("/masters/sync-dept-names", methods=["POST"])
@amgr
def sync_dept_names():
    """
    Fix department name mismatches between master_departments and employees table.
    Compares employee.department values against master list (case-insensitive),
    and updates employees to use the exact master name.
    """
    conn = get_db()
    try:
        # Get all active master department names
        masters = conn.execute(
            "SELECT id, name FROM master_departments WHERE is_active=1"
        ).fetchall()
        master_map = {m["name"].strip().lower(): m["name"] for m in masters}

        # Get all unique department values from employees
        emp_depts = conn.execute(
            "SELECT DISTINCT department FROM employees WHERE department IS NOT NULL AND department != ''"
        ).fetchall()

        fixed = 0
        mismatches = []
        for row in emp_depts:
            dept_val = row["department"] or ""
            dept_lower = dept_val.strip().lower()
            if dept_lower in master_map:
                correct_name = master_map[dept_lower]
                if dept_val != correct_name:
                    # Mismatch found — update to master name
                    conn.execute(
                        "UPDATE employees SET department=? WHERE department=?",
                        (correct_name, dept_val)
                    )
                    # Sync dept_manpower and dept_ot_limits too
                    try:
                        conn.execute("UPDATE dept_manpower SET department=? WHERE department=?", (correct_name, dept_val))
                        conn.execute("UPDATE dept_ot_limits SET department=? WHERE department=?", (correct_name, dept_val))
                    except: pass
                    mismatches.append(f"'{dept_val}' → '{correct_name}'")
                    fixed += 1

        conn.commit()
        conn.close()
        return jsonify({
            "success": True,
            "fixed": fixed,
            "changes": mismatches,
            "message": f"Fixed {fixed} department name mismatches." if fixed else "No mismatches found — all departments are in sync."
        })
    except Exception as e:
        conn.close()
        return jsonify({"success": False, "error": str(e)})


@app.route("/masters/get-all")
def masters_get_all():
    """API to get all masters for dropdown population"""
    conn = get_db()
    data = {
        "categories":   [r["name"] for r in conn.execute("SELECT name FROM master_categories   WHERE is_active=1 ORDER BY name").fetchall()],
        "departments":  [r["name"] for r in conn.execute("SELECT name FROM master_departments  WHERE is_active=1 ORDER BY name").fetchall()],
        "designations": [r["name"] for r in conn.execute("SELECT name FROM master_designations WHERE is_active=1 ORDER BY name").fetchall()],
        "locations":    [r["name"] for r in conn.execute("SELECT name FROM master_locations    WHERE is_active=1 ORDER BY name").fetchall()],
    }
    conn.close()
    return jsonify(data)


# ─── MASTERS ─────────────────────────────────────────



@app.route("/debug/import-mismatch", methods=["POST"])
def debug_import_mismatch():
    """Debug: compare machine employee IDs with software employee IDs"""
    d = request.json
    ip       = d.get("ip","192.168.0.125")
    port     = int(d.get("port",4370))
    password = int(d.get("password",0))
    try:
        from zk import ZK
        zk   = ZK(ip, port=port, timeout=60, password=password, force_udp=False, ommit_ping=True)
        czk  = zk.connect()
        logs = czk.get_attendance()
        czk.disconnect()
        
        # Get unique machine IDs
        machine_ids = sorted(set(str(l.user_id).strip() for l in logs))
        
        # Get software employee codes
        conn = get_db()
        sw_codes = [r["emp_code"] for r in conn.execute("SELECT emp_code FROM employees WHERE status='Active'").fetchall()]
        conn.close()
        
        # Find mismatches
        matched = []
        unmatched = []
        for mid in machine_ids[:50]:  # first 50
            found = mid in sw_codes or mid.zfill(4) in sw_codes or mid.lstrip("0") in sw_codes or (mid.lstrip("0") or "0") in sw_codes
            if found:
                matched.append(mid)
            else:
                unmatched.append(mid)
        
        return jsonify({
            "machine_total_logs": len(logs),
            "machine_unique_ids": len(machine_ids),
            "software_employee_count": len(sw_codes),
            "sample_machine_ids": machine_ids[:20],
            "sample_software_codes": sw_codes[:20],
            "matched_sample": matched[:10],
            "unmatched_sample": unmatched[:10],
            "total_unmatched": len(unmatched)
        })
    except Exception as e:
        return jsonify({"error": str(e)})


@app.route("/attendance/reconcile", methods=["POST"])
@amgr  
def reconcile_attendance():
    """
    Fix: attendance emp_codes jo employees table mein exact match nahi karte
    Sirf exact match wale dikhenge - baaki ka notification aayega
    """
    conn = get_db()
    
    # Find attendance codes with NO exact employee match
    unmatched = conn.execute("""
        SELECT DISTINCT a.emp_code, COUNT(*) as rec_count
        FROM attendance a
        WHERE NOT EXISTS (
            SELECT 1 FROM employees e 
            WHERE e.emp_code = a.emp_code AND e.status='Active'
        )
        GROUP BY a.emp_code
        ORDER BY a.emp_code
    """).fetchall()
    
    total_att   = conn.execute("SELECT COUNT(DISTINCT emp_code) FROM attendance").fetchone()[0]
    total_emps  = conn.execute("SELECT COUNT(*) FROM employees WHERE status='Active'").fetchone()[0]
    matched_att = conn.execute("""
        SELECT COUNT(DISTINCT a.emp_code) FROM attendance a
        WHERE EXISTS (SELECT 1 FROM employees e WHERE e.emp_code=a.emp_code AND e.status='Active')
    """).fetchone()[0]
    
    conn.close()
    
    return jsonify({
        "success":         True,
        "total_employees": total_emps,
        "matched_codes":   matched_att,
        "unmatched_codes": len(unmatched),
        "unmatched_list":  [{"code": r["emp_code"], "records": r["rec_count"]} for r in unmatched[:50]]
    })

@app.route("/attendance/sync-check")
@amgr
def attendance_sync_check():
    """Quick check - kaun se employees ka attendance hai, kaun ka nahi"""
    conn = get_db()
    month = int(request.args.get("month", date.today().month))
    year  = int(request.args.get("year",  date.today().year))
    
    # Employees with NO attendance this month
    no_att = conn.execute("""
        SELECT e.emp_code, e.emp_name, e.department
        FROM employees e
        WHERE e.status='Active'
        AND NOT EXISTS (
            SELECT 1 FROM attendance a 
            WHERE a.emp_code=e.emp_code
            AND strftime('%m',a.att_date)=?
            AND strftime('%Y',a.att_date)=?
        )
        ORDER BY e.emp_name
    """, (f"{month:02d}", str(year))).fetchall()
    
    # Attendance codes with no employee match
    ghost_codes = conn.execute("""
        SELECT DISTINCT a.emp_code, COUNT(*) as cnt
        FROM attendance a
        WHERE NOT EXISTS (
            SELECT 1 FROM employees e WHERE e.emp_code=a.emp_code
        )
        AND strftime('%m',a.att_date)=?
        AND strftime('%Y',a.att_date)=?
        GROUP BY a.emp_code
    """, (f"{month:02d}", str(year))).fetchall()
    
    conn.close()
    return jsonify({
        "success": True,
        "month": month, "year": year,
        "employees_no_attendance": [dict(r) for r in no_att],
        "ghost_codes": [dict(r) for r in ghost_codes]
    })

# ─── AUTO IMPORT SCHEDULER ────────────────────────────────────────────────

# ─────────────────────────────────────────────────────
#  MANPOWER MASTER ROUTES
# ─────────────────────────────────────────────────────
# manpower routes moved to /manpower


def run_auto_import():
    """Background thread: auto import from all machines daily"""
    import time
    from datetime import datetime as _dt

    def do_import():
        try:
            from zk import ZK
            from collections import defaultdict
            conn = get_db()
            machines = conn.execute("SELECT * FROM machines WHERE is_active=1").fetchall()
            conn.close()
            if not machines:
                return
            total = 0
            for machine in machines:
                try:
                    zk  = ZK(machine["ip_address"], port=machine["port"],
                             timeout=30, password=machine["password"] or 0,
                             force_udp=False, ommit_ping=True)
                    czk = zk.connect()
                    czk.disable_device()
                    logs = czk.get_attendance()
                    czk.enable_device()
                    czk.disconnect()

                    from collections import defaultdict
                    daily = defaultdict(list)
                    m_now = _dt.now().month
                    y_now = _dt.now().year
                    for log in logs:
                        if log.timestamp.month == m_now and log.timestamp.year == y_now:
                            daily[(str(log.user_id), log.timestamp.strftime("%Y-%m-%d"))].append(
                                log.timestamp.strftime("%H:%M"))

                    # Night-shift next-day OUT merge (roster-aware)
                    conn2 = get_db()
                    all_shifts_auto = get_all_shifts(conn2)
                    imported = 0
                    emp_punches_auto = {}
                    for (uid_k, att_d_k), times_k in daily.items():
                        if uid_k not in emp_punches_auto:
                            emp_punches_auto[uid_k] = []
                        from datetime import datetime as _dtt_auto
                        m_now2 = _dt.now().month; y_now2 = _dt.now().year
                        for t_k in times_k:
                            try:
                                emp_punches_auto[uid_k].append(
                                    _dtt_auto.strptime(f"{att_d_k} {t_k}", "%Y-%m-%d %H:%M"))
                            except: pass
                    for uid_raw, punches in emp_punches_auto.items():
                        try:
                            emp, matched_uid = find_emp_by_machine_id(conn2, uid_raw)
                            if not emp: continue
                            ec_a = matched_uid; category = emp["category"]

                            # Date filter removed — was blocking same-day auto import.
                            # Manual entry protection handled below via is_manual check.
                            if not punches:
                                continue

                            grp_shifts_a = []
                            try:
                                gr = conn2.execute("""SELECT s.* FROM shifts s
                                    JOIN shift_group_members sgm ON s.id=sgm.shift_id
                                    JOIN employee_shift_groups esg ON sgm.group_id=esg.group_id
                                    WHERE esg.emp_code=? AND s.is_active=1""", (ec_a,)).fetchall()
                                grp_shifts_a = [dict(r) for r in gr]
                            except: pass
                            use_sh = grp_shifts_a if grp_shifts_a else all_shifts_auto
                            pairs_a = etimetrack_pair_punches(sorted(set(punches)), use_sh)
                            for pair in pairs_a:
                                att_date2 = pair["date"]
                                try:
                                    from datetime import date as _dtc2
                                    att_d2 = _dtc2.fromisoformat(att_date2)
                                    auto_status = "WOP" if att_d2.weekday()==get_emp_weekly_off_num(ec_a) else "Present"
                                except: auto_status = "Present"
                                # MANUAL ENTRY PROTECTION: skip if record was manually edited
                                try:
                                    _man_chk = conn2.execute(
                                        "SELECT is_manual FROM attendance WHERE emp_code=? AND att_date=?",
                                        (ec_a, att_date2)).fetchone()
                                    if _man_chk and (_man_chk["is_manual"] or 0) == 1:
                                        continue  # never overwrite manual entries in auto-import
                                except: pass
                                save_att_row(conn2, ec_a, att_date2,
                                             pair["in_time"], pair["out_time"],
                                             category, status=auto_status)
                                imported += 1
                        except: pass
                    conn2.commit()
                    # WAL checkpoint: merge WAL into main DB so readers get fresh data
                    conn2.execute("PRAGMA wal_checkpoint(PASSIVE)")
                    conn2.close()
                    total += imported
                    print(f"[AUTO IMPORT] {machine['machine_name']}: {imported} records")
                except Exception as me:
                    print(f"[AUTO IMPORT] {machine['machine_name']} error: {me}")

            print(f"[AUTO IMPORT] Done! Total: {total} records — {_dt.now().strftime('%d-%b %H:%M')}")

        except Exception as e:
            print(f"[AUTO IMPORT] Failed: {e}")

    # Get configured time (default 08:00)
    def get_auto_time():
        try:
            conn = get_db()
            s = conn.execute("SELECT value FROM app_settings WHERE key='auto_import_time'").fetchone()
            conn.close()
            return s["value"] if s else "08:00"
        except:
            return "08:00"

    print("[AUTO IMPORT] Scheduler started! (Every 15 minutes)")
    last_run_time = None

    while True:
        try:
            now = _dt.now()

            # Run every 15 minutes — reduces DB lock contention significantly
            if last_run_time is None or (now - last_run_time).total_seconds() >= 900:
                do_import()
                last_run_time = now

            time.sleep(60)  # Check every 60 sec (trigger is every 15 min)
        except Exception as e:
            print(f"[AUTO IMPORT] Scheduler error: {e}")
            time.sleep(60)

# Thread is started AFTER init_db() in __main__ block below


@app.route("/machines/auto-import-settings", methods=["GET","POST"])
@amgr
def auto_import_settings():
    conn = get_db()
    try: conn.execute("CREATE TABLE IF NOT EXISTS app_settings (key TEXT PRIMARY KEY, value TEXT)")
    except: pass
    if request.method == "POST":
        d = request.json
        t = d.get("auto_import_time","08:00")
        conn.execute("INSERT OR REPLACE INTO app_settings (key,value) VALUES ('auto_import_time',?)", (t,))
        conn.commit()
        conn.close()
        return jsonify({"success":True,"message":f"Auto import set to {t} daily"})
    s = conn.execute("SELECT value FROM app_settings WHERE key='auto_import_time'").fetchone()
    conn.close()
    return jsonify({"time": s["value"] if s else "08:00"})


# ─── ADMS PUSH SERVER ────────────────────────────────
# eSSL/ZK Machine ADMS mode mein khud data push karti hai
# Machine setting: Server = 192.168.0.3, Port = 89
# Yeh routes port 5000 pe kaam karenge (Flask ke andar)
# Machines page mein ADMS status bhi dikhega

@app.route("/iclock/cdata", methods=["GET", "POST"])
def adms_cdata():
    """
    ADMS Handshake — Machine pehle yahan connect karti hai
    Machine GET request se check karti hai server alive hai ya nahi
    Phir POST se attendance data bhejti hai
    """
    sn = request.args.get("SN", "")  # Machine Serial Number

    if request.method == "GET":
        # Machine ko green signal do — connected!
        now = datetime.now()
        now_str = now.strftime("%Y-%m-%d %H:%M")
        resp = (
            f"GET OPTION FROM: {sn}\n"
            f"ATTLOGStamp=9999\n"
            f"OPERLOGStamp=9999\n"
            f"ATTPHOTOStamp=9999\n"
            f"ErrorDelay=30\n"
            f"Delay=10\n"
            f"TransTimes=00:00;14:05\n"
            f"TransInterval=1\n"
            f"TransFlag=TransData AttLog OpLog AttPhoto\n"
            f"Realtime=1\n"
            f"Encrypt=None\n"
        )
        # SN se machine match karo — update adms_last_seen + connection_mode
        try:
            conn = get_db()
            # Try SN match first
            updated = conn.execute("""UPDATE machines SET 
                adms_last_seen=?, connection_mode='adms', serial_number=?
                WHERE serial_number=?""", (now_str, sn, sn)).rowcount
            if not updated:
                # Fallback: ip_address ya name se match
                conn.execute("""UPDATE machines SET 
                    adms_last_seen=?, connection_mode='adms', serial_number=COALESCE(NULLIF(serial_number,''),?)
                    WHERE ip_address=? OR machine_name LIKE ?""",
                    (now_str, sn, request.remote_addr, f"%{sn}%"))
            conn.commit(); conn.close()
        except: pass
        print(f"[ADMS] Heartbeat from SN={sn} ({request.remote_addr})")
        return resp, 200, {"Content-Type": "text/plain"}

    elif request.method == "POST":
        # Machine attendance data bhej rahi hai
        raw = request.get_data(as_text=True)
        lines = raw.strip().split("\n")

        imported = 0; errors = []
        conn = get_db()

        for line in lines:
            line = line.strip()
            if not line: continue
            # ADMS format: EmpCode\tDate Time\tInOut\t...\n
            # Example: 35\t2026-03-27 08:05:00\t0\t\t\n
            parts = line.split("\t")
            if len(parts) < 2: continue
            try:
                emp_code_raw = parts[0].strip()
                datetime_str = parts[1].strip()  # "2026-03-27 08:05:00"
                in_out_flag  = parts[2].strip() if len(parts) > 2 else ""

                # Parse datetime
                try:
                    dt_obj = datetime.strptime(datetime_str, "%Y-%m-%d %H:%M:%S")
                except:
                    try:
                        dt_obj = datetime.strptime(datetime_str, "%Y-%m-%d %H:%M")
                    except:
                        continue

                att_date   = dt_obj.strftime("%Y-%m-%d")
                punch_time = dt_obj.strftime("%H:%M")

                # Employee match karo
                emp, matched_code = find_emp_by_machine_id(conn, emp_code_raw)
                if not emp:
                    continue

                ec       = matched_code
                category = emp["category"]

                # InOutFlag: 0=IN, 1=OUT, 4=Break Out, 5=Break In
                is_in  = in_out_flag in ["0", "in", ""]
                is_out = in_out_flag in ["1", "out", "4"]

                # Existing record check
                existing = conn.execute(
                    "SELECT in_time, out_time FROM attendance WHERE emp_code=? AND att_date=?",
                    (ec, att_date)).fetchone()

                if existing:
                    curr_in  = existing["in_time"]  or ""
                    curr_out = existing["out_time"] or ""

                    def _tm(s):
                        try: h,m=map(int,str(s).split(":")[:2]); return h*60+m
                        except: return -1

                    if is_in:
                        # IN punch: pehla IN rakho
                        new_in = punch_time if (not curr_in or _tm(punch_time) < _tm(curr_in)) else curr_in
                        new_out = curr_out
                    elif is_out:
                        # OUT punch: aakhri OUT rakho
                        new_in  = curr_in
                        new_out = punch_time if (not curr_out or _tm(punch_time) > _tm(curr_out)) else curr_out
                    else:
                        # Unknown flag — treat as any punch
                        new_in  = curr_in  or punch_time
                        new_out = punch_time if curr_in else curr_out

                    # Auto status
                    try:
                        att_dt = date.fromisoformat(att_date)
                        if att_dt.weekday() == get_emp_weekly_off_num(ec):
                            auto_status = "WOP"
                        else:
                            hol = conn.execute("""SELECT id FROM holidays WHERE holiday_date=?
                                AND (applies_to='All' OR applies_to IS NULL OR applies_to='' OR applies_to=? OR applies_to='Staff' OR applies_to='Associate')""",
                                (att_date, category)).fetchone()
                            auto_status = "Holiday" if hol else "Present"
                    except:
                        auto_status = "Present"

                    save_att_row(conn, ec, att_date, new_in, new_out, category, status=auto_status)
                else:
                    # Naya record
                    try:
                        att_dt = date.fromisoformat(att_date)
                        if att_dt.weekday() == get_emp_weekly_off_num(ec):
                            auto_status = "WOP"
                        else:
                            hol = conn.execute("""SELECT id FROM holidays WHERE holiday_date=?
                                AND (applies_to='All' OR applies_to IS NULL OR applies_to='' OR applies_to=? OR applies_to='Staff' OR applies_to='Associate')""",
                                (att_date, category)).fetchone()
                            auto_status = "Holiday" if hol else "Present"
                    except:
                        auto_status = "Present"

                    if is_in:
                        save_att_row(conn, ec, att_date, punch_time, "", category, status=auto_status)
                    elif is_out:
                        save_att_row(conn, ec, att_date, "", punch_time, category, status=auto_status)
                    else:
                        save_att_row(conn, ec, att_date, punch_time, "", category, status=auto_status)

                imported += 1
                if imported % 50 == 0:
                    conn.commit()

            except Exception as e:
                errors.append(str(e)[:50])
                continue

        conn.commit()
        # Machine ka sync count update karo
        try:
            conn.execute("""UPDATE machines SET last_sync=?, last_sync_count=last_sync_count+?
                WHERE ip_address=?""",
                (datetime.now().strftime("%Y-%m-%d %H:%M"), imported, request.remote_addr))
            conn.commit()
        except: pass
        conn.close()

        # Update last_sync by SN
        try:
            sn_post = request.args.get("SN", "")
            now_str2 = datetime.now().strftime("%Y-%m-%d %H:%M")
            updated2 = conn.execute("""UPDATE machines SET 
                last_sync=?, adms_last_seen=?, last_sync_count=last_sync_count+?,
                connection_mode='adms'
                WHERE serial_number=?""",
                (now_str2, now_str2, imported, sn_post)).rowcount
            if not updated2:
                conn.execute("""UPDATE machines SET 
                    last_sync=?, adms_last_seen=?, last_sync_count=last_sync_count+?,
                    connection_mode='adms'
                    WHERE ip_address=?""",
                    (now_str2, now_str2, imported, request.remote_addr))
            conn.commit()
        except: pass
        conn.close()
        print(f"[ADMS] {request.remote_addr} SN={request.args.get('SN','')} → {imported} records saved")
        return "OK", 200, {"Content-Type": "text/plain"}

    return "OK", 200


@app.route("/iclock/getrequest", methods=["GET"])
def adms_getrequest():
    """Machine commands fetch karti hai yahan se"""
    return "OK", 200, {"Content-Type": "text/plain"}


@app.route("/iclock/devicecmd", methods=["POST"])
def adms_devicecmd():
    """Machine command acknowledgment"""
    return "OK", 200, {"Content-Type": "text/plain"}


@app.route("/adms/status")
@amgr
def adms_status():
    """ADMS se kitni machines connected hain aur last sync kab tha"""
    conn = get_db()
    machines = conn.execute("""SELECT * FROM machines 
        WHERE last_sync IS NOT NULL 
        ORDER BY last_sync DESC""").fetchall()
    conn.close()
    result = []
    for m in machines:
        last = m["last_sync"] or ""
        # Check if last sync was in last 5 minutes = online
        try:
            last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M")
            diff_min = (datetime.now() - last_dt).seconds // 60
            is_online = diff_min <= 5
        except:
            is_online = False
        result.append({
            "name":      m["machine_name"],
            "ip":        m["ip_address"],
            "last_sync": last,
            "is_online": is_online,
            "count":     m["last_sync_count"] or 0
        })
    return jsonify(result)


# ─── MAIN ────────────────────────────────────────────
@app.before_request
def log_page_visit():
    if "user" not in session: return
    if request.method != "GET": return
    path = request.path
    skip = ["/static","/api/","/favicon",".js",".css",".png",".ico",".jpg","/letters/header","/letters/footer"]
    if any(s in path for s in skip): return
    try:
        conn=get_db()
        conn.execute("INSERT INTO activity_log (username,action,page,ip_address) VALUES (?,?,?,?)",
            (session.get("user",""),"Page Visit",path,request.remote_addr))
        conn.commit(); conn.close()
    except: pass


def run_auto_recalculate():
    """Background thread: recalculate attendance for today+yesterday.
    Runs every 10 minutes, only between 07:00-22:00 to reduce DB load."""
    import time
    from datetime import datetime as _dt, date as _date, timedelta as _td

    INTERVAL_SECONDS = 600        # 10 minutes — was 1 minute (10x less DB load)
    ACTIVE_HOUR_START = 7         # Start recalc only after 7 AM
    ACTIVE_HOUR_END   = 22        # Stop recalc after 10 PM

    print("[AUTO RECALC] Scheduler started! (Every 10 min, 07:00–22:00)")
    last_run = None

    while True:
        try:
            now = _dt.now()
            hour = now.hour

            # Only run during active hours
            if not (ACTIVE_HOUR_START <= hour < ACTIVE_HOUR_END):
                time.sleep(60)   # Check again in 1 min during off-hours
                continue

            elapsed = (now - last_run).total_seconds() if last_run else INTERVAL_SECONDS + 1
            if elapsed >= INTERVAL_SECONDS:
                try:
                    today     = _date.today()
                    yesterday = today - _td(days=1)

                    conn = get_db()
                    emps = conn.execute(
                        "SELECT emp_code FROM employees WHERE status='Active'"
                    ).fetchall()
                    conn.close()

                    # Use single shared connection for entire batch — avoids 1140 open/close
                    shared_conn = get_db()
                    recalc_count = 0
                    for emp in emps:
                        try:
                            recalc_att_day_conn(shared_conn, emp["emp_code"], today.strftime("%Y-%m-%d"))
                            recalc_att_day_conn(shared_conn, emp["emp_code"], yesterday.strftime("%Y-%m-%d"))
                            recalc_count += 1
                        except Exception:
                            pass
                    shared_conn.commit()
                    shared_conn.close()

                    print(f"[AUTO RECALC] Done! {recalc_count} employees — {now.strftime('%d-%b %H:%M')}")
                    last_run = now

                except Exception as e:
                    print(f"[AUTO RECALC] Error: {e}")
                    last_run = now

            time.sleep(60)   # Poll every 60 sec, but only execute every 10 min
        except Exception as e:
            print(f"[AUTO RECALC] Scheduler error: {e}")
            time.sleep(60)


def recalc_att_day_conn(conn, emp_code, att_date):
    """Recalculate using a pre-existing shared connection (no open/close overhead)."""
    try:
        emp = conn.execute("SELECT * FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
        if not emp: return
        from datetime import datetime as _dtt
        try:
            d_obj = _dtt.strptime(att_date, "%Y-%m-%d")
            is_emp_wo_day = (d_obj.weekday() == get_emp_weekly_off_num(emp_code, conn))
        except:
            is_emp_wo_day = False
        rec = conn.execute(
            "SELECT in_time, out_time, status FROM attendance WHERE emp_code=? AND att_date=?",
            (emp_code, att_date)).fetchone()
        in_t   = (rec["in_time"]  or "") if rec else ""
        out_t  = (rec["out_time"] or "") if rec else ""
        status = (rec["status"]   or "") if rec else ""
        if is_emp_wo_day and not in_t:
            if rec:
                if status != "WO":
                    conn.execute("UPDATE attendance SET status='WO', working_minutes=0, ot_minutes=0, late_minutes=0, short_minutes=0 WHERE emp_code=? AND att_date=?",
                        (emp_code, att_date))
            else:
                conn.execute("""INSERT OR IGNORE INTO attendance
                    (emp_code,att_date,in_time,out_time,working_minutes,status,late_minutes,short_minutes,ot_minutes,is_half_day)
                    VALUES (?,?,'','',0,'WO',0,0,0,0)""", (emp_code, att_date))
            return
        if is_emp_wo_day and in_t:
            save_att_row(conn, emp_code, att_date, in_t, out_t or None, emp["category"], status="WOP", status_override="WOP")
            return
        if not rec or not in_t: return
        # Do NOT overwrite Leave status — leave is manually assigned
        if status == "Leave":
            return
        save_att_row(conn, emp_code, att_date, in_t, out_t or None, emp["category"],
                     status=status if status in ("WOP","HP","Holiday","Leave") else "Present")
    except Exception:
        pass

def recalc_att_day(emp_code, att_date):
    """
    Recalculate attendance for one employee for one date.
    Uses save_att_row which handles shift auto-detection + missing punch logic.
    Also fixes WO day based on employee's weekly_off setting.
    """
    conn = get_db()
    try:
        emp = conn.execute("SELECT * FROM employees WHERE emp_code=?", (emp_code,)).fetchone()
        if not emp:
            conn.close(); return

        # Check if this date is employee's weekly off day
        from datetime import datetime as _dtt
        try:
            d_obj = _dtt.strptime(att_date, "%Y-%m-%d")
            is_emp_wo_day = (d_obj.weekday() == get_emp_weekly_off_num(emp_code, conn))
        except:
            is_emp_wo_day = False

        # Get existing record
        rec = conn.execute(
            "SELECT in_time, out_time, status FROM attendance WHERE emp_code=? AND att_date=?",
            (emp_code, att_date)).fetchone()

        in_t   = (rec["in_time"]  or "") if rec else ""
        out_t  = (rec["out_time"] or "") if rec else ""
        status = (rec["status"]   or "") if rec else ""

        # Case 1: Employee's WO day with NO punch → must be WO (not Absent)
        if is_emp_wo_day and not in_t:
            if rec:
                if status != "WO":
                    conn.execute("UPDATE attendance SET status='WO', working_minutes=0, ot_minutes=0, late_minutes=0, short_minutes=0 WHERE emp_code=? AND att_date=?",
                        (emp_code, att_date))
                    conn.commit()
            else:
                conn.execute("""INSERT OR IGNORE INTO attendance
                    (emp_code,att_date,in_time,out_time,working_minutes,status,late_minutes,short_minutes,ot_minutes,is_half_day)
                    VALUES (?,?,'','',0,'WO',0,0,0,0)""", (emp_code, att_date))
                conn.commit()
            conn.close(); return

        # Case 2: Employee's WO day WITH punch → WOP (OT calculated)
        if is_emp_wo_day and in_t:
            save_att_row(conn, emp_code, att_date,
                         in_t, out_t or None,
                         emp["category"],
                         status="WOP", status_override="WOP")
            conn.commit()
            conn.close(); return

        # Case 3: Normal working day — no punch record
        if not rec:
            conn.close(); return

        # Case 4: Normal working day — has record, recalculate
        if not in_t:
            # No punch on working day
            if status in ("Holiday", "Leave"):
                conn.close(); return  # Keep Holiday/Leave as-is
            conn.close(); return

        # Case 5: Has IN punch → full recalculate via save_att_row
        save_att_row(conn, emp_code, att_date,
                     in_t, out_t or None,
                     emp["category"],
                     status=status if status in ("WOP","HP","Holiday","Leave") else "Present")
        conn.commit()
    except Exception as e:
        pass
    finally:
        conn.close()

def run_auto_leave_earn():
    """Background thread: Auto credit monthly EL+CL on 1st of every month"""
    import time
    from datetime import datetime as _dt
    print("[AUTO LEAVE] Scheduler started — will credit on 1st of each month")
    _last_credited_month = None
    while True:
        try:
            now = _dt.now()
            today = now.date()
            # Run on 1st of month, only once per month
            if today.day == 1 and _last_credited_month != (today.year, today.month):
                conn = get_db()
                try:
                    ps = get_payroll_settings(conn)
                    el_per_year = float(ps.get("el_per_year", 16) or 16)
                    cl_per_year = float(ps.get("cl_per_year", 6) or 6)
                    el_monthly  = round(el_per_year / 12, 4)
                    cl_monthly  = round(cl_per_year / 12, 4)
                    m = today.month; y = today.year
                    emps = conn.execute("SELECT emp_code, category FROM employees WHERE status='Active'").fetchall()
                    credited = 0
                    for e in emps:
                        ec = e["emp_code"]
                        # Skip if already credited this month
                        already = conn.execute(
                            "SELECT id FROM leave_earn_log WHERE emp_code=? AND year=? AND month=?",
                            (ec, y, m)).fetchone()
                        if already: continue
                        conn.execute("INSERT OR IGNORE INTO leave_balance (emp_code,year) VALUES (?,?)", (ec, y))
                        if e["category"] == "Staff":
                            conn.execute("""UPDATE leave_balance SET
                                el_allotted=ROUND(el_allotted+?,2),
                                cl_allotted=ROUND(cl_allotted+?,2)
                                WHERE emp_code=? AND year=?""", (el_monthly, cl_monthly, ec, y))
                            conn.execute("""INSERT OR IGNORE INTO leave_earn_log
                                (emp_code,year,month,el_credited,cl_credited,credited_on,source)
                                VALUES (?,?,?,?,?,datetime('now'),'auto')""",
                                (ec, y, m, el_monthly, cl_monthly))
                        else:
                            conn.execute("""UPDATE leave_balance SET
                                el_allotted=ROUND(el_allotted+?,2) WHERE emp_code=? AND year=?""",
                                (el_monthly, ec, y))
                            conn.execute("""INSERT OR IGNORE INTO leave_earn_log
                                (emp_code,year,month,el_credited,cl_credited,credited_on,source)
                                VALUES (?,?,?,?,0,datetime('now'),'auto')""",
                                (ec, y, m, el_monthly))
                        credited += 1
                    conn.commit()
                    _last_credited_month = (y, m)
                    print(f"[AUTO LEAVE] {today.strftime('%d-%b %Y')}: EL +{el_monthly}/head, CL(Staff) +{cl_monthly}/head — {credited} employees credited")
                finally:
                    conn.close()
        except Exception as e:
            print(f"[AUTO LEAVE] Error: {e}")
        time.sleep(3600)  # Check every hour


if __name__ == "__main__":
    init_db()
    # ✅ Start background thread AFTER init_db() — prevents "database is locked"
    _auto_thread = threading.Thread(target=run_auto_import, daemon=True)
    _auto_thread.start()
    # Auto-recalculate attendance every 2 minutes
    _recalc_thread = threading.Thread(target=run_auto_recalculate, daemon=True)
    _recalc_thread.start()
    # Auto leave earn — credits on 1st of every month
    _leave_earn_thread = threading.Thread(target=run_auto_leave_earn, daemon=True)
    _leave_earn_thread.start()
    print("\n" + "="*55)
    print("  VIJAYSHRI PACKAGING LTD. — PAYROLL SYSTEM")
    print("="*55)
    print("  Browser : http://localhost:5000")
    print("  LAN     : http://192.168.0.39:5000")
    print("  ADMS    : http://192.168.0.3:5000  (Port 89 → 5000)")
    print("  Login   : admin / Admin@123")
    print("="*55 + "\n")
    app.run(host="0.0.0.0", port=5000, debug=False)
