import os
import re
import io
import sqlite3
import datetime
from difflib import get_close_matches

import pandas as pd
import streamlit as st
import altair as alt

# -----------------------------------------------------------------------------
# Configuration & Constants
# -----------------------------------------------------------------------------
DATA_DIR = "./data"
os.makedirs(DATA_DIR, exist_ok=True)
DB_FILENAME = os.path.join(DATA_DIR, "edwin_county_farms_sales.db")

# Fallback Column Mappings for CSV Imports
DEFAULT_COLUMN_MAPPINGS = {
    'date_fulfilled': ("Fulfillment Date", ['fulfillment date', 'fulfillment']),
    'date_placed': ("Date Placed", ['date placed', 'placed', 'date']),
    'order_id': ("Order ID", ['order id', 'order']),
    'customer': ("Customer Name", ['customer name', 'customer']),
    'product_id': ("Product ID", ['product id', 'item id']),
    'product': ("Product", ['product name', 'product', 'item']),
    'category': ("Category", ['category']),
    'sub_category': ("Sub-Category", ['subcategory', 'sub-category', 'sub category']),
    'order_status': ("Order Status", ['order status', 'status']),
    'item_unit': ("Item Unit", ['item unit', 'unit']),
    'num_items': ("# of Items", ['# of items', 'num items', 'items']),
    'qty': ("Quantity", ['qty', 'quantity']),
    'subtotal': ("Product Subtotal", ['product subtotal', 'subtotal']),
    'total': ("Order Total", ['order total', 'total', 'price']),
}


# -----------------------------------------------------------------------------
# Helper Functions & Utilities
# -----------------------------------------------------------------------------
def parse_float_safe(val, default=0.0):
    """Safely converts arbitrary input or currency strings to float."""
    if pd.isna(val) or val is None:
        return default
    try:
        clean_str = str(val).replace('$', '').replace('€', '').replace(' ', '').replace(',', '.')
        return float(clean_str)
    except (ValueError, TypeError):
        return default


def safe_parse_date(date_val):
    """Safely converts arbitrary date strings/objects to python datetime.date."""
    if pd.isna(date_val) or not date_val:
        return datetime.date.today()
    try:
        return pd.to_datetime(date_val).date()
    except Exception:
        return datetime.date.today()


def read_csv_robust(file_path_or_buffer):
    """Reads CSV with encoding and delimiter fallbacks."""
    for encoding, sep in [('utf-8', ','), ('utf-16', '\t'), ('windows-1252', ';')]:
        try:
            if hasattr(file_path_or_buffer, 'seek'):
                file_path_or_buffer.seek(0)
            return pd.read_csv(file_path_or_buffer, encoding=encoding, sep=sep)
        except Exception:
            continue
    raise ValueError("Could not parse CSV file with supported encodings.")


def resolve_column(df_columns, primary_name, fallback_keywords):
    """
    Resolves column names matching exact names, fallback keywords, or partial matches.
    Guards against overlapping words (e.g. avoiding 'Product ID' when searching for 'Product').
    """
    cols_clean = [str(c).strip() for c in df_columns]
    cols_lower = [c.lower() for c in cols_clean]

    # 1. Exact match for primary_name
    p_lower = primary_name.strip().lower()
    if p_lower in cols_lower:
        return df_columns[cols_lower.index(p_lower)]

    # 2. Exact match for fallback keywords
    for kw in fallback_keywords:
        kw_lower = kw.strip().lower()
        if kw_lower in cols_lower:
            return df_columns[cols_lower.index(kw_lower)]

    # 3. Partial match with safeguards
    for kw in fallback_keywords:
        kw_lower = kw.strip().lower()
        for idx, col_lower in enumerate(cols_lower):
            if kw_lower in col_lower:
                if 'id' not in kw_lower and ' id' in col_lower:
                    continue
                return df_columns[idx]

    return primary_name


def resolve_effective_subcategory(row):
    """Returns SubCategory if valid, or falls back to ProductName if uncategorized/blank."""
    cat = str(row.get('Category', '')).strip().lower()
    subcat = str(row.get('SubCategory', '')).strip().lower()
    uncat_terms = {'uncategorized', 'uncategorize', 'none', 'nan', ''}

    if cat in uncat_terms or subcat in uncat_terms or pd.isna(row.get('SubCategory')):
        return str(row.get('ProductName', 'Unknown Product'))
    return str(row.get('SubCategory')).strip()


# -----------------------------------------------------------------------------
# Database Operations
# -----------------------------------------------------------------------------
def get_db_connection():
    """Returns a SQLite connection context manager."""
    return sqlite3.connect(DB_FILENAME)


def init_db():
    """Initializes the database schema and performs migrations."""
    with get_db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Customers (
                CustomerID INTEGER PRIMARY KEY AUTOINCREMENT,
                Name TEXT UNIQUE
            )
        ''')
        cursor.execute("INSERT OR IGNORE INTO Customers (CustomerID, Name) VALUES (1, 'Market')")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Products (
                ProductID TEXT PRIMARY KEY,
                Name TEXT,
                BaseUnit TEXT,
                Category TEXT,
                SubCategory TEXT
            )
        ''')

        # Migrations
        cursor.execute("PRAGMA table_info(Products)")
        existing_cols = [col[1] for col in cursor.fetchall()]
        if 'Category' not in existing_cols:
            cursor.execute("ALTER TABLE Products ADD COLUMN Category TEXT")
        if 'SubCategory' not in existing_cols:
            cursor.execute("ALTER TABLE Products ADD COLUMN SubCategory TEXT")

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Orders (
                OrderID TEXT PRIMARY KEY,
                CustomerID INTEGER,
                DatePlaced TEXT,
                DateFullfilled TEXT,
                FOREIGN KEY(CustomerID) REFERENCES Customers(CustomerID)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS Sales (
                SaleID INTEGER PRIMARY KEY AUTOINCREMENT,
                OrderID TEXT,
                ProductID TEXT,
                BaseQuantitySold REAL,
                SalePrice REAL,
                FOREIGN KEY(OrderID) REFERENCES Orders(OrderID),
                FOREIGN KEY(ProductID) REFERENCES Products(ProductID)
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ProductMappings (
                ImportName TEXT PRIMARY KEY,
                ProductID TEXT,
                FOREIGN KEY(ProductID) REFERENCES Products(ProductID)
            )
        ''')
        conn.commit()


def save_product_mapping(import_name, product_id):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO ProductMappings (ImportName, ProductID) 
            VALUES (?, ?)
            ON CONFLICT(ImportName) DO UPDATE SET ProductID = excluded.ProductID
        ''', (import_name, product_id))
        conn.commit()


def get_product_mapping(import_name):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ProductID FROM ProductMappings WHERE ImportName = ?', (import_name,))
        row = cursor.fetchone()
        return row[0] if row else None


def get_all_mappings_df():
    with get_db_connection() as conn:
        return pd.read_sql_query('''
            SELECT 
                pm.ImportName, 
                pm.ProductID, 
                p.Name AS DBProductName
            FROM ProductMappings pm
            LEFT JOIN Products p ON pm.ProductID = p.ProductID
        ''', conn)


def get_registered_order_ids():
    if not os.path.exists(DB_FILENAME):
        return set()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT OrderID FROM Orders")
        return {row[0] for row in cursor.fetchall()}


def get_or_create_customer(name):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT CustomerID FROM Customers WHERE Name = ?', (name,))
        row = cursor.fetchone()
        if row:
            customer_id = row[0]
        else:
            cursor.execute('INSERT INTO Customers (Name) VALUES (?)', (name,))
            customer_id = cursor.lastrowid
        conn.commit()
        return customer_id


def get_all_customers():
    with get_db_connection() as conn:
        return pd.read_sql_query('SELECT CustomerID, Name FROM Customers ORDER BY Name', conn)


def get_or_create_product(product_id, name, base_unit=None, category=None, sub_category=None):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT ProductID FROM Products WHERE ProductID = ?', (product_id,))
        row = cursor.fetchone()
        if row:
            updates, params = [], []
            if name is not None:
                updates.append("Name = ?"); params.append(name)
            if base_unit is not None:
                updates.append("BaseUnit = ?"); params.append(base_unit)
            if category is not None:
                updates.append("Category = ?"); params.append(category)
            if sub_category is not None:
                updates.append("SubCategory = ?"); params.append(sub_category)

            if updates:
                params.append(product_id)
                cursor.execute(f"UPDATE Products SET {', '.join(updates)} WHERE ProductID = ?", params)
        else:
            unit = base_unit if base_unit is not None else "each"
            cat = category if category is not None else "Uncategorized"
            sub_cat = sub_category if sub_category is not None else "Uncategorized"
            cursor.execute(
                'INSERT INTO Products (ProductID, Name, BaseUnit, Category, SubCategory) VALUES (?, ?, ?, ?, ?)',
                (product_id, name, unit, cat, sub_cat)
            )
        conn.commit()
        return product_id


def get_all_products():
    with get_db_connection() as conn:
        df_products = pd.read_sql_query(
            'SELECT ProductID, Name, BaseUnit, Category, SubCategory FROM Products ORDER BY Name', conn)

    if not df_products.empty:
        df_products['Display'] = df_products['ProductID'].astype(str) + " - " + df_products['Name']
        return df_products
    return pd.DataFrame(columns=['ProductID', 'Name', 'BaseUnit', 'Category', 'SubCategory', 'Display'])


def get_or_create_order(order_id, customer_id, date_placed, date_fulfilled):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT OrderID FROM Orders WHERE OrderID = ?', (order_id,))
        row = cursor.fetchone()
        if not row:
            cursor.execute('''
                INSERT INTO Orders (OrderID, CustomerID, DatePlaced, DateFullfilled) 
                VALUES (?, ?, ?, ?)
            ''', (order_id, customer_id, date_placed, date_fulfilled))
        else:
            cursor.execute('''
                UPDATE Orders SET CustomerID = ?, DatePlaced = ?, DateFullfilled = ? WHERE OrderID = ?
            ''', (customer_id, date_placed, date_fulfilled, order_id))
        conn.commit()
        return order_id


def create_sale(order_id, product_id, base_quantity_sold, sale_price):
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO Sales (OrderID, ProductID, BaseQuantitySold, SalePrice) 
            VALUES (?, ?, ?, ?)
        ''', (order_id, product_id, base_quantity_sold, sale_price))
        conn.commit()


def load_sales_analytics_data():
    if not os.path.exists(DB_FILENAME):
        return pd.DataFrame()

    query = '''
        SELECT 
            s.SaleID,
            o.OrderID,
            o.DateFullfilled AS FulfillmentDate,
            o.DatePlaced AS DatePlaced,
            c.CustomerID,
            c.Name AS CustomerName,
            p.ProductID,
            p.Name AS ProductName,
            p.BaseUnit,
            COALESCE(p.Category, 'Uncategorized') AS Category,
            COALESCE(p.SubCategory, 'Uncategorized') AS SubCategory,
            s.BaseQuantitySold,
            s.SalePrice
        FROM Sales s
        JOIN Orders o ON s.OrderID = o.OrderID
        JOIN Customers c ON o.CustomerID = c.CustomerID
        JOIN Products p ON s.ProductID = p.ProductID
    '''
    try:
        with get_db_connection() as conn:
            df = pd.read_sql_query(query, conn)
    except Exception:
        df = pd.DataFrame()

    if not df.empty:
        df['FulfillmentDatetime'] = pd.to_datetime(df['FulfillmentDate'], errors='coerce')
        df['FulfillmentDateObj'] = df['FulfillmentDatetime'].dt.date
        df['Week'] = df['FulfillmentDatetime'].dt.to_period('W').astype(str)
        df['Month'] = df['FulfillmentDatetime'].dt.to_period('M').astype(str)
        df['Year'] = df['FulfillmentDatetime'].dt.year.fillna(0).astype(int)

    return df


def parse_csv_to_scratch(external_df, db_products_df=None):
    items = []
    db_names = []
    db_map = {}
    if db_products_df is not None and not db_products_df.empty:
        db_names = db_products_df['Name'].tolist()
        for _, r in db_products_df.iterrows():
            db_map[str(r['Name']).strip().lower()] = (str(r['ProductID']), str(r['Name']))

    try:
        with get_db_connection() as conn:
            mapping_rows = conn.cursor().execute("SELECT ImportName, ProductID FROM ProductMappings").fetchall()
        mappings_dict = {row[0]: row[1] for row in mapping_rows}
    except Exception:
        mappings_dict = {}

    for _, row in external_df.iterrows():
        name = str(row.get('Nom de l’article', row.get('Product', row.get('Item', '')))).strip()
        variant = str(row.get('Variante d’article', row.get('Variant', ''))).strip()

        if variant and variant.lower() not in ['aucune description', 'regular', 'nan', 'none']:
            import_name = f"{name}\n{variant}"
            search_query = f"{name} {variant}".strip()
        else:
            import_name = name if name and name != 'nan' else 'Unknown Product'
            search_query = import_name

        matched_prod_id = ""
        matched_prod_name = ""
        lookup_key = name.lower()

        if import_name in mappings_dict:
            matched_prod_id = mappings_dict[import_name]
            if db_products_df is not None and not db_products_df.empty:
                m_row = db_products_df[db_products_df['ProductID'] == matched_prod_id]
                if not m_row.empty:
                    matched_prod_name = str(m_row.iloc[0]['Name'])
        elif lookup_key in db_map:
            matched_prod_id, matched_prod_name = db_map[lookup_key]
        elif db_names:
            close_matches = get_close_matches(search_query, db_names, n=1, cutoff=0.3)
            if close_matches:
                matched_name = close_matches[0]
                for _, r in db_products_df.iterrows():
                    if str(r['Name']).strip() == matched_name:
                        matched_prod_id = str(r['ProductID'])
                        matched_prod_name = str(r['Name'])
                        break
            if not matched_prod_name and not db_products_df.empty:
                matched_prod_id = str(db_products_df.iloc[0]['ProductID'])
                matched_prod_name = str(db_products_df.iloc[0]['Name'])

        qty = 1.0
        for q_col in ['Unités vendues', 'Articles vendus', 'Quantity', 'Qty']:
            if q_col in row and pd.notna(row[q_col]):
                qty = parse_float_safe(row[q_col], default=1.0)
                break

        price = 0.0
        for p_col in ['Ventes nettes', 'Ventes brutes', 'Subtotal', 'Total', 'Price']:
            if p_col in row and pd.notna(row[p_col]):
                price = parse_float_safe(row[p_col], default=0.0)
                break

        items.append({
            "Select": False,
            "ImportName": import_name,
            "ProductID": matched_prod_id,
            "ProductName": matched_prod_name,
            "BaseQuantitySold": qty,
            "SalePrice": price
        })
    return pd.DataFrame(items)


# -----------------------------------------------------------------------------
# Dialogs
# -----------------------------------------------------------------------------
@st.dialog("Merge Products")
def merge_dialog(selected_df):
    st.warning("Merging will update all historical sales and mappings to point to the kept product. The other products will be permanently deleted.")

    st.write("Selected Products to Merge:")
    st.dataframe(selected_df[['ProductID', 'Name', 'Category', 'SubCategory']], hide_index=True)

    keep_options = selected_df['ProductID'].astype(str) + " - " + selected_df['Name']
    target_selection = st.selectbox("Which product do you want to KEEP as the primary?", keep_options.tolist())

    if st.button("🚨 Confirm Merge", type="primary", use_container_width=True):
        target_pid = target_selection.split(" - ", 1)[0]
        other_pids = selected_df[selected_df['ProductID'] != target_pid]['ProductID'].tolist()

        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                placeholders = ','.join(['?'] * len(other_pids))

                cursor.execute(f"UPDATE Sales SET ProductID = ? WHERE ProductID IN ({placeholders})", [target_pid] + other_pids)
                cursor.execute(f"UPDATE ProductMappings SET ProductID = ? WHERE ProductID IN ({placeholders})", [target_pid] + other_pids)
                cursor.execute(f"DELETE FROM Products WHERE ProductID IN ({placeholders})", other_pids)
                conn.commit()

            st.success("Products merged successfully!")
            st.rerun()
        except Exception as e:
            st.error(f"Error merging products: {e}")


@st.dialog("Merge Customers")
def merge_customers_dialog(selected_df):
    st.warning("Merging will update all historical orders and sales to point to the selected primary customer. The merged customer(s) will be permanently deleted.")

    st.write("Selected Customers to Merge:")
    st.dataframe(selected_df[['CustomerID', 'Name']], hide_index=True)

    keep_options = selected_df['CustomerID'].astype(str) + " - " + selected_df['Name']
    target_selection = st.selectbox("Which customer do you want to KEEP as primary?", keep_options.tolist())

    if st.button("🚨 Confirm Customer Merge", type="primary", use_container_width=True):
        target_cid = int(target_selection.split(" - ", 1)[0])
        other_cids = [int(cid) for cid in selected_df[selected_df['CustomerID'] != target_cid]['CustomerID'].tolist()]

        try:
            with get_db_connection() as conn:
                cursor = conn.cursor()
                placeholders = ','.join(['?'] * len(other_cids))

                cursor.execute(f"UPDATE Orders SET CustomerID = ? WHERE CustomerID IN ({placeholders})", [target_cid] + other_cids)
                cursor.execute(f"DELETE FROM Customers WHERE CustomerID IN ({placeholders})", other_cids)
                conn.commit()

            st.success("Customers merged successfully!")
            st.rerun()
        except Exception as e:
            st.error(f"Error merging customers: {e}")


@st.dialog("Define New Products", width="large")
def define_new_products_dialog(import_names):
    st.write("Please set the properties for the newly mapped products below:")
    with st.form("new_products_form"):
        new_prods = []
        for i, csv_name in enumerate(import_names):
            st.markdown(f"**Original CSV Name:** `{csv_name}`")
            c1, c2, c3, c4, c5 = st.columns(5)

            slug = re.sub(r'[^a-z0-9]+', '-', str(csv_name).lower()).strip('-')
            if not slug:
                slug = "item"
            default_id = f"{slug}-{datetime.datetime.now().strftime('%M%S')}-{i}"

            pid = c1.text_input("Product ID", value=default_id, key=f"np_id_{i}")
            pname = c2.text_input("Product Name", value=str(csv_name), key=f"np_name_{i}")
            punit = c3.text_input("Unit", value="each", key=f"np_unit_{i}")
            pcat = c4.text_input("Category", value="Produce", key=f"np_cat_{i}")
            psub = c5.text_input("Sub-Category", value="Uncategorized", key=f"np_sub_{i}")
            new_prods.append((csv_name, pid, pname, punit, pcat, psub))

        if st.form_submit_button("💾 Save New Products"):
            for csv_name, pid, pname, punit, pcat, psub in new_prods:
                clean_pid = pid.strip()
                clean_pname = pname.strip()
                get_or_create_product(clean_pid, clean_pname, punit.strip(), pcat.strip(), psub.strip())
                save_product_mapping(csv_name, clean_pid)

                display_str = f"{clean_pid} - {clean_pname}"
                mask = st.session_state["market_draft_df"]["ImportName"] == csv_name
                st.session_state["market_draft_df"].loc[mask, "Selected DB Product"] = display_str

            st.success("New products created! Please re-click 'Aggregate & Save Market Order' to finalize the import.")
            st.rerun()


# -----------------------------------------------------------------------------
# Main Application Setup
# -----------------------------------------------------------------------------
st.set_page_config(page_title="Edwin County Farms - Sales Management", layout="wide")
init_db()

st.title("Edwin County Farms - Sales Management System")

is_local = st.secrets.get("ENVIRONMENT") != "cloud"

# Tab setup based on environment
tab_titles = [
    "📝 Sales Entry & Validation",
    "📊 Sales Analysis Dashboard",
    "🥬 Product Management",
    "👥 Customer Management",
    "📦 Order Management"
]

if is_local:
    tab1, tab2, tab3, tab_cust, tab4 = st.tabs(tab_titles)
else:
    # Public online view restrictions
    tabs = st.tabs(["📊 Sales Analysis Dashboard"])
    tab2 = tabs[0]
    tab1, tab3, tab_cust, tab4 = None, None, None, None, None


# =============================================================================
# TAB 1: SALES ENTRY & VALIDATION (LOCAL ONLY)
# =============================================================================
if is_local and tab1:
    with tab1:
        st.header("📝 Sales Entry & Validation")
        uploaded_tab1_csv = st.file_uploader(
            "Upload Sales Export CSV for Validation",
            type=["csv"],
            key="tab1_csv_uploader"
        )

        df_raw = None
        if uploaded_tab1_csv is not None:
            local_csv_path = os.path.join(DATA_DIR, uploaded_tab1_csv.name)
            with open(local_csv_path, "wb") as f:
                f.write(uploaded_tab1_csv.getbuffer())

            st.success(f"File saved locally: `{local_csv_path}`")

            try:
                df_raw = read_csv_robust(local_csv_path)
            except Exception as e:
                st.error(f"Error loading CSV file: {e}")

        else:
            st.info("Please upload a CSV file above to review and validate pending sales orders.")

        if df_raw is not None:
            cols = list(df_raw.columns)

            # Resolve column mappings
            date_fulfilled_col = resolve_column(cols, *DEFAULT_COLUMN_MAPPINGS['date_fulfilled'])
            date_placed_col = resolve_column(cols, *DEFAULT_COLUMN_MAPPINGS['date_placed'])
            order_id_col = resolve_column(cols, *DEFAULT_COLUMN_MAPPINGS['order_id'])
            customer_col = resolve_column(cols, *DEFAULT_COLUMN_MAPPINGS['customer'])
            product_id_col = resolve_column(cols, *DEFAULT_COLUMN_MAPPINGS['product_id'])
            product_col = resolve_column(cols, *DEFAULT_COLUMN_MAPPINGS['product'])
            category_col = resolve_column(cols, *DEFAULT_COLUMN_MAPPINGS['category'])
            sub_category_col = resolve_column(cols, *DEFAULT_COLUMN_MAPPINGS['sub_category'])
            order_status_col = resolve_column(cols, *DEFAULT_COLUMN_MAPPINGS['order_status'])
            item_unit_col = resolve_column(cols, *DEFAULT_COLUMN_MAPPINGS['item_unit'])
            num_items_col = resolve_column(cols, *DEFAULT_COLUMN_MAPPINGS['num_items'])
            qty_col = resolve_column(cols, *DEFAULT_COLUMN_MAPPINGS['qty'])
            subtotal_col = resolve_column(cols, *DEFAULT_COLUMN_MAPPINGS['subtotal'])
            total_col = resolve_column(cols, *DEFAULT_COLUMN_MAPPINGS['total'])

            # Fill missing optional columns with sensible defaults to prevent KeyErrors
            defaults = {
                order_status_col: "Fulfilled",
                category_col: "Uncategorized",
                sub_category_col: "Uncategorized",
                item_unit_col: "each",
                num_items_col: 1.0,
                qty_col: 1.0,
                subtotal_col: 0.0,
                total_col: 0.0,
            }
            for col_name, default_val in defaults.items():
                if col_name not in df_raw.columns:
                    df_raw[col_name] = default_val

            registered_db_orders = get_registered_order_ids()
            valid_df = df_raw[~df_raw[order_status_col].astype(str).str.upper().str.contains('CANCEL', na=False)].copy()

            all_raw_dates = valid_df[date_fulfilled_col].dropna().unique()
            truly_pending_dates = []

            for d in all_raw_dates:
                df_d = valid_df[valid_df[date_fulfilled_col] == d]
                is_guest_d = df_d[customer_col].str.startswith("Guest customer", na=False)
                df_reg_d = df_d[~is_guest_d]
                df_gst_d = df_d[is_guest_d]

                reg_pending = not df_reg_d[order_id_col].astype(str).isin(registered_db_orders).all() if not df_reg_d.empty else False
                guest_pending = (not df_gst_d.empty) and (f"GUEST-{d}" not in registered_db_orders)

                if reg_pending or guest_pending:
                    truly_pending_dates.append(d)

            if len(truly_pending_dates) == 0:
                st.success("✨ All valid records have been registered!")
            else:
                db_products_df_bulk = get_all_products()
                valid_db_pids = set(db_products_df_bulk['ProductID'].astype(str).tolist()) if not db_products_df_bulk.empty else set()

                try:
                    with get_db_connection() as conn:
                        mapping_rows = conn.cursor().execute("SELECT ImportName, ProductID FROM ProductMappings").fetchall()
                    mappings_dict = {row[0]: row[1] for row in mapping_rows}
                except Exception:
                    mappings_dict = {}

                auto_ready_orders = []

                for d in truly_pending_dates:
                    df_date = valid_df[valid_df[date_fulfilled_col] == d]

                    is_guest = df_date[customer_col].str.startswith("Guest customer", na=False)
                    df_regular = df_date[~is_guest]
                    df_guests = df_date[is_guest]

                    guest_order_id = f"GUEST-{d}"
                    if not df_guests.empty and guest_order_id not in registered_db_orders:
                        grouped = df_guests.groupby([product_id_col, product_col, item_unit_col, category_col, sub_category_col])
                        guest_sales = []
                        all_guest_mapped = True

                        for (p_id, p_name, p_unit, p_cat, p_subcat), group in grouped:
                            csv_prod_name = str(p_name)
                            csv_prod_id = str(p_id)

                            mapped_pid = mappings_dict.get(csv_prod_name)
                            if mapped_pid and mapped_pid in valid_db_pids:
                                final_pid = mapped_pid
                            elif csv_prod_id in valid_db_pids:
                                final_pid = csv_prod_id
                            else:
                                all_guest_mapped = False
                                break

                            total_base_qty = 0.0
                            total_subtotal = 0.0
                            for _, row in group.iterrows():
                                ni = parse_float_safe(row.get(num_items_col), default=1.0)
                                q = parse_float_safe(row.get(qty_col), default=0.0)
                                total_base_qty += (ni * q)
                                total_subtotal += parse_float_safe(row.get(subtotal_col), default=0.0)

                            guest_sales.append({
                                'prod_id': final_pid,
                                'qty_sold': total_base_qty,
                                'sale_price': total_subtotal
                            })

                        if all_guest_mapped and guest_sales:
                            auto_ready_orders.append({
                                'order_id': guest_order_id,
                                'customer_name': "Market",
                                'date_placed': str(safe_parse_date(df_guests.iloc[0][date_placed_col])),
                                'date_fulfilled': str(safe_parse_date(d)),
                                'sales': guest_sales
                            })

                    df_regular_pending = df_regular[~df_regular[order_id_col].astype(str).isin(registered_db_orders)]
                    for o_id, o_df in df_regular_pending.groupby(order_id_col):
                        all_regular_mapped = True
                        regular_sales = []

                        for _, row in o_df.iterrows():
                            csv_prod_id = str(row[product_id_col])
                            csv_prod_name = str(row[product_col])

                            mapped_pid = mappings_dict.get(csv_prod_name)
                            if mapped_pid and mapped_pid in valid_db_pids:
                                final_pid = mapped_pid
                            elif csv_prod_id in valid_db_pids:
                                final_pid = csv_prod_id
                            else:
                                all_regular_mapped = False
                                break

                            ni = parse_float_safe(row.get(num_items_col), default=1.0)
                            q = parse_float_safe(row.get(qty_col), default=0.0)
                            calc_base_qty = ni * q
                            subtotal_val = parse_float_safe(row.get(subtotal_col), default=0.0)

                            regular_sales.append({
                                'prod_id': final_pid,
                                'qty_sold': calc_base_qty,
                                'sale_price': subtotal_val
                            })

                        if all_regular_mapped and regular_sales:
                            auto_ready_orders.append({
                                'order_id': str(o_id),
                                'customer_name': str(o_df.iloc[0][customer_col]),
                                'date_placed': str(safe_parse_date(o_df.iloc[0][date_placed_col])),
                                'date_fulfilled': str(safe_parse_date(o_df.iloc[0][date_fulfilled_col])),
                                'sales': regular_sales
                            })

                if len(auto_ready_orders) > 0:
                    st.info(f"✨ Found **{len(auto_ready_orders)}** order(s) where all products are natively mapped to the database.")
                    if st.button(f"🚀 Bulk Validate & Save Ready Orders ({len(auto_ready_orders)})", type="primary"):
                        with st.spinner("Saving mapped orders..."):
                            for o_data in auto_ready_orders:
                                c_id = get_or_create_customer(o_data['customer_name'])
                                get_or_create_order(o_data['order_id'], c_id, o_data['date_placed'], o_data['date_fulfilled'])
                                for sale in o_data['sales']:
                                    create_sale(o_data['order_id'], sale['prod_id'], sale['qty_sold'], sale['sale_price'])
                            st.success(f"Successfully processed {len(auto_ready_orders)} orders!")
                            st.rerun()
                    st.divider()

                try:
                    sorted_dates = sorted(truly_pending_dates, key=pd.to_datetime, reverse=True)
                except Exception:
                    sorted_dates = sorted(truly_pending_dates, reverse=True)

                selected_date = st.selectbox("1. Select Fulfillment Date to Review Manually", sorted_dates)
                df_date = valid_df[valid_df[date_fulfilled_col] == selected_date]

                is_guest = df_date[customer_col].str.startswith("Guest customer", na=False)
                df_regular = df_date[~is_guest]
                df_guests = df_date[is_guest]

                guest_order_id = f"GUEST-{selected_date}"
                market_already_registered = guest_order_id in registered_db_orders

                guest_aggregated = []
                if not df_guests.empty and not market_already_registered:
                    grouped = df_guests.groupby([product_id_col, product_col, item_unit_col, category_col, sub_category_col])
                    for (p_id, p_name, p_unit, p_cat, p_subcat), group in grouped:
                        total_base_qty = 0.0
                        total_subtotal = 0.0

                        for idx, row in group.iterrows():
                            ni = parse_float_safe(row.get(num_items_col), default=1.0)
                            q = parse_float_safe(row.get(qty_col), default=0.0)
                            total_base_qty += (ni * q)
                            total_subtotal += parse_float_safe(row.get(subtotal_col), default=0.0)

                        guest_aggregated.append({
                            'product_id': str(p_id),
                            'product_name': str(p_name),
                            'item_unit': str(p_unit) if pd.notna(p_unit) else "each",
                            'base_qty_sold': total_base_qty,
                            'sale_price': total_subtotal
                        })

                df_regular_pending = df_regular[~df_regular[order_id_col].astype(str).isin(registered_db_orders)]
                regular_order_ids = sorted(df_regular_pending[order_id_col].dropna().unique())

                guest_order_key = f"GUEST_BATCH_{selected_date}"

                dropdown_order_options = []
                if not df_guests.empty and not market_already_registered:
                    dropdown_order_options.append(guest_order_key)
                dropdown_order_options.extend(regular_order_ids)

                if len(dropdown_order_options) == 0:
                    st.info(f"All orders and market sales for {selected_date} have already been registered in the database.")
                else:
                    def format_order_display(o_id):
                        if o_id == guest_order_key:
                            total_guest_sum = sum([item['sale_price'] for item in guest_aggregated])
                            d_placed = df_guests.iloc[0][date_placed_col]
                            return f"🛒 [GUEST BATCH] Market | Total: ${total_guest_sum:.2f} | Placed: {d_placed}"
                        else:
                            o_df = df_regular_pending[df_regular_pending[order_id_col] == o_id]
                            c_name = o_df.iloc[0][customer_col]
                            d_placed = o_df.iloc[0][date_placed_col]
                            total_val = parse_float_safe(o_df.iloc[0][total_col], default=0.0)
                            return f"Order {o_id} | {c_name} | Total: ${total_val:.2f} | Placed: {d_placed}"

                    selected_order = st.selectbox("2. Select Order Number", dropdown_order_options, format_func=format_order_display)

                    if selected_order:
                        try:
                            db_products_df = get_all_products()
                            product_display_list = db_products_df['Display'].tolist() if not db_products_df.empty else []
                        except Exception:
                            product_display_list = []
                            db_products_df = pd.DataFrame(columns=['ProductID', 'Name', 'BaseUnit', 'Category', 'SubCategory', 'Display'])

                        st.divider()
                        is_guest_selection = (selected_order == guest_order_key)

                        if is_guest_selection:
                            st.markdown(f"### 📦 Consolidated Guest Order: {selected_date}")
                            with st.container():
                                c1, c2, c3, c4 = st.columns(4)
                                customer_input = c1.text_input("Customer Name", value="Market")
                                order_input = c2.text_input("Order ID", value=guest_order_id)
                                date_p_input = c3.date_input("Date Placed", value=safe_parse_date(df_guests.iloc[0][date_placed_col]))
                                date_f_input = c4.date_input("Date Fulfilled", value=safe_parse_date(selected_date))
                        else:
                            df_order = df_regular_pending[df_regular_pending[order_id_col] == selected_order]
                            st.markdown(f"### 📦 Order Details: {selected_order}")
                            first_row = df_order.iloc[0]
                            with st.container():
                                c1, c2, c3, c4 = st.columns(4)
                                customer_input = c1.text_input("Customer Name", value=str(first_row[customer_col]))
                                order_input = c2.text_input("Order ID", value=str(selected_order))
                                date_p_input = c3.date_input("Date Placed", value=safe_parse_date(first_row[date_placed_col]))
                                date_f_input = c4.date_input("Date Fulfilled", value=safe_parse_date(first_row[date_fulfilled_col]))

                        st.divider()
                        st.markdown("### 🛒 Sales within this Order")
                        sale_inputs = []

                        if is_guest_selection:
                            for idx, item in enumerate(guest_aggregated):
                                with st.container():
                                    csv_prod_id = item['product_id']
                                    csv_prod_name = item['product_name']
                                    csv_item_unit = item['item_unit']
                                    calc_base_qty = item['base_qty_sold']
                                    subtotal_val = item['sale_price']

                                    st.markdown(f"📌 **CSV Item:** `{csv_prod_name}`")

                                    sc1, sc2, sc3, sc4, sc5, sc6 = st.columns([1.8, 1.2, 1.2, 0.8, 0.8, 0.8])

                                    dropdown_options = ["++ Create New Product ++"] + product_display_list
                                    mapped_pid = get_product_mapping(csv_prod_name)
                                    if mapped_pid:
                                        matched_db_row = db_products_df[db_products_df['ProductID'].astype(str) == mapped_pid]
                                    else:
                                        matched_db_row = db_products_df[db_products_df['ProductID'].astype(str) == csv_prod_id]

                                    default_idx = dropdown_options.index(matched_db_row.iloc[0]['Display']) if not matched_db_row.empty else 0

                                    selected_db_product = sc1.selectbox(f"Database Mapping [Guest {idx}]", dropdown_options, index=default_idx, key=f"g_dd_{idx}")

                                    if selected_db_product == "++ Create New Product ++":
                                        final_prod_id = sc2.text_input("New DB ID", value=csv_prod_id, key=f"g_pid_{idx}")
                                        final_prod_name = sc3.text_input("New DB Name", value=csv_prod_name, key=f"g_pn_{idx}")
                                        base_unit = sc4.text_input("Unit", value=csv_item_unit, key=f"g_bu_{idx}")
                                    else:
                                        selected_id = selected_db_product.split(" - ", 1)[0]
                                        selected_name = selected_db_product.split(" - ", 1)[1] if " - " in selected_db_product else ""
                                        matched_row = db_products_df[db_products_df['ProductID'].astype(str) == selected_id]
                                        db_unit_val = matched_row.iloc[0]['BaseUnit'] if not matched_row.empty else csv_item_unit

                                        final_prod_id, final_prod_name, base_unit = selected_id, selected_name, db_unit_val

                                        sc2.text_input("Matched DB ID", value=final_prod_id, disabled=True, key=f"g_pid_{idx}")
                                        sc3.text_input("Matched DB Name", value=final_prod_name, disabled=True, key=f"g_pn_{idx}")
                                        sc4.text_input("DB Unit", value=base_unit, disabled=True, key=f"g_bu_{idx}")

                                    qty_sold_input = sc5.number_input("Qty", value=float(calc_base_qty), step=0.1, key=f"g_qs_{idx}")
                                    sale_price_input = sc6.number_input("Price ($)", value=float(subtotal_val), step=1.0, key=f"g_sp_{idx}")

                                    sale_inputs.append({
                                        'original_name': csv_prod_name,
                                        'prod_id': final_prod_id,
                                        'prod_name': final_prod_name,
                                        'base_unit': base_unit,
                                        'category': None,
                                        'sub_category': None,
                                        'qty_sold': qty_sold_input,
                                        'sale_price': sale_price_input
                                    })
                                    st.divider()
                        else:
                            df_order = df_regular_pending[df_regular_pending[order_id_col] == selected_order]
                            for idx, row in df_order.iterrows():
                                with st.container():
                                    csv_prod_id = str(row[product_id_col])
                                    csv_prod_name = str(row[product_col])
                                    csv_item_unit = str(row[item_unit_col]) if pd.notna(row[item_unit_col]) else "each"

                                    num_items = parse_float_safe(row.get(num_items_col), default=1.0)
                                    qty = parse_float_safe(row.get(qty_col), default=0.0)
                                    calc_base_qty = num_items * qty
                                    subtotal_val = parse_float_safe(row.get(subtotal_col), default=0.0)

                                    st.markdown(f"📌 **CSV Item:** `{csv_prod_name}`")

                                    sc1, sc2, sc3, sc4, sc5, sc6 = st.columns([1.8, 1.2, 1.2, 0.8, 0.8, 0.8])

                                    dropdown_options = ["++ Create New Product ++"] + product_display_list
                                    mapped_pid = get_product_mapping(csv_prod_name)
                                    if mapped_pid:
                                        matched_db_row = db_products_df[db_products_df['ProductID'].astype(str) == mapped_pid]
                                    else:
                                        matched_db_row = db_products_df[db_products_df['ProductID'].astype(str) == csv_prod_id]

                                    default_idx = dropdown_options.index(matched_db_row.iloc[0]['Display']) if not matched_db_row.empty else 0

                                    selected_db_product = sc1.selectbox(f"Database Mapping [{idx}]", dropdown_options, index=default_idx, key=f"dd_{idx}")

                                    if selected_db_product == "++ Create New Product ++":
                                        final_prod_id = sc2.text_input("New DB ID", value=csv_prod_id, key=f"pid_{idx}")
                                        final_prod_name = sc3.text_input("New DB Name", value=csv_prod_name, key=f"pn_{idx}")
                                        base_unit = sc4.text_input("Unit", value=csv_item_unit, key=f"bu_{idx}")
                                    else:
                                        selected_id = selected_db_product.split(" - ", 1)[0]
                                        selected_name = selected_db_product.split(" - ", 1)[1] if " - " in selected_db_product else ""
                                        matched_row = db_products_df[db_products_df['ProductID'].astype(str) == selected_id]
                                        db_unit_val = matched_row.iloc[0]['BaseUnit'] if not matched_row.empty else csv_item_unit

                                        final_prod_id, final_prod_name, base_unit = selected_id, selected_name, db_unit_val

                                        sc2.text_input("Matched DB ID", value=final_prod_id, disabled=True, key=f"pid_{idx}")
                                        sc3.text_input("Matched DB Name", value=final_prod_name, disabled=True, key=f"pn_{idx}")
                                        sc4.text_input("DB Unit", value=base_unit, disabled=True, key=f"bu_{idx}")

                                    qty_sold_input = sc5.number_input("Qty", value=float(calc_base_qty), step=0.1, key=f"qs_{idx}")
                                    sale_price_input = sc6.number_input("Price ($)", value=float(subtotal_val), step=1.0, key=f"sp_{idx}")

                                    sale_inputs.append({
                                        'original_name': csv_prod_name,
                                        'prod_id': final_prod_id,
                                        'prod_name': final_prod_name,
                                        'base_unit': base_unit,
                                        'category': None,
                                        'sub_category': None,
                                        'qty_sold': qty_sold_input,
                                        'sale_price': sale_price_input
                                    })
                                    st.divider()

                        if st.button("✅ Validate & Save Order", type="primary", use_container_width=True):
                            try:
                                c_id = get_or_create_customer(customer_input)
                                o_id = get_or_create_order(order_input, c_id, str(date_p_input), str(date_f_input))
                                for sale in sale_inputs:
                                    p_id = get_or_create_product(sale['prod_id'], sale['prod_name'], sale['base_unit'], sale['category'], sale['sub_category'])
                                    create_sale(o_id, p_id, sale['qty_sold'], sale['sale_price'])
                                    if sale['original_name']:
                                        save_product_mapping(sale['original_name'], p_id)
                                st.success(f"Successfully processed Order {order_input} with {len(sale_inputs)} items!")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Error saving to database: {e}")


# =============================================================================
# TAB 2: SALES ANALYSIS & PLANNING DASHBOARD
# =============================================================================
with tab2:
    st.header("📈 Sales Analytics")

    analytics_df = load_sales_analytics_data()

    if analytics_df.empty:
        st.info("No sales records found in the database yet. Validate and save orders in Tab 1 or Tab 5 to view analytics.")
    else:
        # ---------------------------------------------------------------------
        # GLOBAL FILTERS (YoY & Seasonal Controls)
        # ---------------------------------------------------------------------
        st.subheader("🔍 Filter Analytics")

        month_options = [
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December"
        ]

        selected_months = st.multiselect(
            "Filter by Month(s)",
            options=month_options,
            default=month_options,
            key="yoy_months_multiselect"
        )

        def map_to_dummy_year(date_obj):
            if pd.isna(date_obj):
                return pd.NaT
            if date_obj.month == 2 and date_obj.day == 29:
                return datetime.date(2024, 2, 29)
            return datetime.date(2024, date_obj.month, date_obj.day)

        yoy_base_df = analytics_df.copy()
        yoy_base_df['MonthName'] = yoy_base_df['FulfillmentDatetime'].dt.month_name()
        yoy_base_df['DummyDate'] = yoy_base_df['FulfillmentDateObj'].apply(map_to_dummy_year)

        # Base filter by selected months
        yoy_filtered = yoy_base_df[yoy_base_df['MonthName'].isin(selected_months)]

        pf_col1, pf_col2, pf_col3, pf_col4, pf_col5 = st.columns(5)

        available_years = sorted(yoy_filtered['Year'].unique().tolist(), reverse=True)
        filter_yoy_years = pf_col1.multiselect("Select Years to Compare", available_years, default=available_years, key="yoy_year")
        filter_yoy_cats = pf_col2.multiselect("Filter by Category", sorted(yoy_filtered['Category'].unique()), default="Produce", key="yoy_cat")
        filter_yoy_subcats = pf_col3.multiselect("Filter by Sub-Category", sorted(yoy_filtered['SubCategory'].unique()), default=[], key="yoy_subcat")
        filter_yoy_custs = pf_col4.multiselect("Filter by Customer", sorted(yoy_filtered['CustomerName'].unique()), default=[], key="yoy_cust")
        filter_yoy_prods = pf_col5.multiselect("Filter by Product", sorted(yoy_filtered['ProductName'].unique()), default=[], key="yoy_prod")

        if filter_yoy_years:
            yoy_filtered = yoy_filtered[yoy_filtered['Year'].isin(filter_yoy_years)]
        if filter_yoy_cats:
            yoy_filtered = yoy_filtered[yoy_filtered['Category'].isin(filter_yoy_cats)]
        if filter_yoy_subcats:
            yoy_filtered = yoy_filtered[yoy_filtered['SubCategory'].isin(filter_yoy_subcats)]
        if filter_yoy_custs:
            yoy_filtered = yoy_filtered[yoy_filtered['CustomerName'].isin(filter_yoy_custs)]
        if filter_yoy_prods:
            yoy_filtered = yoy_filtered[yoy_filtered['ProductName'].isin(filter_yoy_prods)]

        st.caption(f"Showing **{len(yoy_filtered)}** sales line items matching current filters.")
        st.divider()

        if yoy_filtered.empty:
            st.warning("No sales found for the selected filter criteria.")
        else:
            # ---------------------------------------------------------------------
            # KEY METRICS CARDS
            # ---------------------------------------------------------------------
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Total Revenue", f"${yoy_filtered['SalePrice'].sum():,.2f}")
            col2.metric("Total Units Sold", f"{yoy_filtered['BaseQuantitySold'].sum():,.1f}")
            col3.metric("Total Orders", f"{yoy_filtered['OrderID'].nunique():,}")
            col4.metric("Unique Customers", f"{yoy_filtered['CustomerName'].nunique():,}")

            st.divider()

            # ---------------------------------------------------------------------
            # 1. TIMELINE TREND CHART
            # ---------------------------------------------------------------------
            target_col = 'SalePrice'
            yoy_metric = "Revenue ($)"

            st.markdown("#### 📈 Revenue ($) Timeline Trend")
            time_view = st.radio(
                "Aggregate Timeline By:",
                ["Daily", "Weekly", "Monthly"],
                index=1,  # Default to Weekly
                horizontal=True,
                key="yoy_time_agg"
            )

            trend_base_df = yoy_filtered.copy()
            if time_view == "Daily":
                trend_base_df['PeriodStart'] = trend_base_df['DummyDate']
            elif time_view == "Weekly":
                trend_base_df['PeriodStart'] = pd.to_datetime(trend_base_df['DummyDate']) - pd.to_timedelta(
                    pd.to_datetime(trend_base_df['DummyDate']).dt.weekday, unit='d')
                trend_base_df['PeriodStart'] = trend_base_df['PeriodStart'].dt.date
            elif time_view == "Monthly":
                trend_base_df['PeriodStart'] = pd.to_datetime(trend_base_df['DummyDate']).apply(
                    lambda x: datetime.date(x.year, x.month, 1))

            agg_df = trend_base_df.groupby(['Year', 'PeriodStart'])[target_col].sum().reset_index()
            agg_df['YearLabel'] = agg_df['Year'].astype(str)

            if time_view == "Monthly":
                agg_df['FullDateLabel'] = pd.to_datetime(agg_df['PeriodStart']).dt.strftime('%B')
                axis_format = '%B'
            else:
                agg_df['FullDateLabel'] = pd.to_datetime(agg_df['PeriodStart']).dt.strftime('%d-%b')
                axis_format = '%b %d'

            line_chart = alt.Chart(agg_df).mark_line(point=True).encode(
                x=alt.X('PeriodStart:T', title='Time of Year', axis=alt.Axis(format=axis_format)),
                y=alt.Y(f'{target_col}:Q', title=yoy_metric),
                color=alt.Color('YearLabel:N', title='Year'),
                tooltip=['FullDateLabel:N', alt.Tooltip(f'{target_col}:Q', format=',.2f'), 'YearLabel:N']
            ).interactive()

            st.altair_chart(line_chart, use_container_width=True)

            st.divider()

            # ---------------------------------------------------------------------
            # 2. REVENUE BY CUSTOMER CHART (TOP N FILTERED - ALWAYS SHOW LABELS)
            # ---------------------------------------------------------------------
            chart_col1, chart_col2 = st.columns(2)

            with chart_col1:
                st.markdown("#### 👥 Revenue by Customer ($)")
                total_cust_count = yoy_filtered['CustomerName'].nunique()
                top_n_custs = st.number_input(
                    "Show Top N Customers",
                    min_value=1,
                    max_value=max(total_cust_count, 1),
                    value=min(5, max(total_cust_count, 1)),
                    step=1,
                    key="top_n_customers_input"
                )

                # Order customers by total revenue across selected period
                cust_totals = yoy_filtered.groupby('CustomerName')['SalePrice'].sum().nlargest(top_n_custs)
                top_customer_names = cust_totals.index.tolist()

                cust_chart_df = yoy_filtered[yoy_filtered['CustomerName'].isin(top_customer_names)].groupby(['CustomerName', 'Year'])['SalePrice'].sum().reset_index()
                cust_chart_df['YearStr'] = cust_chart_df['Year'].astype(str)
                cust_chart_df['CustomerName'] = pd.Categorical(cust_chart_df['CustomerName'], categories=top_customer_names, ordered=True)

                cust_chart = alt.Chart(cust_chart_df).mark_bar().encode(
                    x=alt.X(
                        'CustomerName:N',
                        title='Customer',
                        sort=top_customer_names,
                        axis=alt.Axis(labelAngle=-45, labelLimit=300, labelOverlap=False)
                    ),
                    xOffset='YearStr:N',
                    y=alt.Y('SalePrice:Q', title='Revenue ($)'),
                    color=alt.Color('YearStr:N', title='Year'),
                    tooltip=['CustomerName:N', 'YearStr:N', alt.Tooltip('SalePrice:Q', format=',.2f')]
                ).configure_view(stroke='transparent')

                st.altair_chart(cust_chart, use_container_width=True)

            # ---------------------------------------------------------------------
            # 3. PRODUCT / SUB-CATEGORY REVENUE COMPARISON CHART (TOP N FILTERED)
            # ---------------------------------------------------------------------
            with chart_col2:
                st.markdown("#### 🥬 Product Revenue Comparison")

                group_by_subcat_chart = st.checkbox(
                    "Group by Sub-Category",
                    value=True,
                    key="prod_chart_subcat_toggle"
                )

                yoy_filtered_chart = yoy_filtered.copy()
                yoy_filtered_chart['EffectiveSubCategory'] = yoy_filtered_chart.apply(resolve_effective_subcategory, axis=1)

                group_col = 'EffectiveSubCategory' if group_by_subcat_chart else 'ProductName'
                group_title = 'Sub-Category' if group_by_subcat_chart else 'Product'

                total_prod_count = yoy_filtered_chart[group_col].nunique()
                top_n_prods = st.number_input(
                    f"Show Top N {group_title}s",
                    min_value=1,
                    max_value=max(total_prod_count, 1),
                    value=min(5, max(total_prod_count, 1)),
                    step=1,
                    key="top_n_products_input"
                )

                # Order items by total revenue across selected period
                prod_totals = yoy_filtered_chart.groupby(group_col)['SalePrice'].sum().nlargest(top_n_prods)
                top_prod_names = prod_totals.index.tolist()

                prod_chart_df = yoy_filtered_chart[yoy_filtered_chart[group_col].isin(top_prod_names)].groupby([group_col, 'Year'])['SalePrice'].sum().reset_index()
                prod_chart_df['YearStr'] = prod_chart_df['Year'].astype(str)
                prod_chart_df[group_col] = pd.Categorical(prod_chart_df[group_col], categories=top_prod_names, ordered=True)

                yoy_prod_chart = alt.Chart(prod_chart_df).mark_bar().encode(
                    x=alt.X(
                        f'{group_col}:N',
                        title=group_title,
                        sort=top_prod_names,
                        axis=alt.Axis(labelAngle=-45, labelLimit=300, labelOverlap=False)
                    ),
                    xOffset='YearStr:N',
                    y=alt.Y('SalePrice:Q', title='Revenue ($)'),
                    color=alt.Color('YearStr:N', title='Year'),
                    tooltip=[f'{group_col}:N', 'YearStr:N', alt.Tooltip('SalePrice:Q', format=',.2f')]
                ).configure_view(stroke='transparent')

                st.altair_chart(yoy_prod_chart, use_container_width=True)

            st.divider()

            # ---------------------------------------------------------------------
            # 4. DATA MATRIX & EXPORT
            # ---------------------------------------------------------------------
            st.write("###### Revenue ($) Data Matrix")
            group_yoy_matrix_subcat = st.checkbox("Group produces by Sub-Category", value=True, key="yoy_matrix_subcat_toggle")

            yoy_matrix_df = yoy_filtered.copy()
            yoy_matrix_df['EffectiveSubCategory'] = yoy_matrix_df.apply(resolve_effective_subcategory, axis=1)

            if group_yoy_matrix_subcat:
                prod_label_map = yoy_matrix_df.groupby(['Category', 'EffectiveSubCategory'])['ProductName'].agg(
                    lambda x: f"{x.nunique()} product(s)" if x.nunique() > 1 else x.iloc[0]
                )
                yoy_matrix_df['ProductNameLabel'] = yoy_matrix_df.set_index(['Category', 'EffectiveSubCategory']).index.map(prod_label_map)
                pivot_index = ['Category', 'EffectiveSubCategory', 'ProductNameLabel']
            else:
                pivot_index = ['Category', 'EffectiveSubCategory', 'ProductName']

            index_names = ['Category', 'SubCategory', 'ProductName']

            yoy_pivot = pd.pivot_table(
                yoy_matrix_df,
                values=target_col,
                index=pivot_index,
                columns='Year',
                aggfunc='sum',
                fill_value=0
            )
            yoy_pivot.index.names = index_names

            if not yoy_pivot.empty:
                col_sums = yoy_pivot.sum(numeric_only=True)
                total_index = pd.MultiIndex.from_tuples([("TOTAL", "", "")], names=index_names)
                total_df = pd.DataFrame([col_sums], index=total_index)
                yoy_pivot_with_total = pd.concat([yoy_pivot, total_df])
            else:
                yoy_pivot_with_total = yoy_pivot

            st.dataframe(yoy_pivot_with_total, use_container_width=True)

            # Export Data Matrix Button
            csv_matrix = yoy_pivot_with_total.to_csv().encode('utf-8')
            st.download_button(
                label="📥 Download Data Matrix as CSV",
                data=csv_matrix,
                file_name=f"YoY_Revenue_Data_Matrix_{datetime.datetime.now().strftime('%Y%m%d')}.csv",
                mime="text/csv",
                key="download_yoy_matrix_btn"
            )


# =============================================================================
# TAB 3: PRODUCT MANAGEMENT (LOCAL ONLY)
# =============================================================================
if is_local and tab3:
    with tab3:
        st.header("🥬 Product Management")

        subtab3_1, subtab3_2 = st.tabs(["🥬 Product Catalog", "🔗 Product Mappings Table"])

        # -------------------------------------------------------------------------
        # SUB-TAB 3.1: PRODUCT CATALOG
        # -------------------------------------------------------------------------
        with subtab3_1:
            st.write("View, create, edit, and merge product characteristics stored in the database.")

            with st.expander("➕ Add New Product", expanded=False):
                with st.form("add_product_form"):
                    ap_col1, ap_col2, ap_col3, ap_col4, ap_col5 = st.columns(5)
                    new_p_id = ap_col1.text_input("Product ID (e.g. carrots-baby)")
                    new_p_name = ap_col2.text_input("Product Name (e.g. Carrots Baby)")
                    new_p_unit = ap_col3.text_input("Base Unit", value="each")
                    new_p_cat = ap_col4.text_input("Category", value="Produce")
                    new_p_subcat = ap_col5.text_input("Sub-Category", value="Uncategorized")

                    submit_new_prod = st.form_submit_button("💾 Save New Product")
                    if submit_new_prod:
                        if not new_p_id.strip() or not new_p_name.strip():
                            st.error("Please provide both a Product ID and Product Name.")
                        else:
                            try:
                                get_or_create_product(new_p_id.strip(), new_p_name.strip(), new_p_unit.strip(),
                                                      new_p_cat.strip(), new_p_subcat.strip())
                                st.success(f"Product '{new_p_name}' created successfully!")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Error creating product: {e}")

            st.divider()

            with get_db_connection() as conn:
                prod_df = pd.read_sql_query("SELECT ProductID, Name, BaseUnit, Category, SubCategory FROM Products ORDER BY Name", conn)

            if prod_df.empty:
                st.info("No products found in the database yet. Process sales in Tab 1 or add products above.")
            else:
                prod_df.insert(0, "Select for Merge", False)

                st.write("Edit the table below directly and click **Save Product Changes** when finished, OR select products to **Merge**.")

                c_merge_btn, _ = st.columns([1, 2])

                edited_prod_df = st.data_editor(
                    prod_df,
                    num_rows="dynamic",
                    use_container_width=True,
                    key="product_editor",
                    column_config={
                        "Select for Merge": st.column_config.CheckboxColumn("Merge", default=False),
                        "ProductID": st.column_config.TextColumn("Product ID", disabled=True),
                        "Name": st.column_config.TextColumn("Product Name"),
                        "BaseUnit": st.column_config.TextColumn("Base Unit (e.g. pound, bunch)"),
                        "Category": st.column_config.TextColumn("Category"),
                        "SubCategory": st.column_config.TextColumn("Sub-Category")
                    }
                )

                selected_for_merge = edited_prod_df[edited_prod_df["Select for Merge"] == True]

                with c_merge_btn:
                    if st.button("🔗 Merge Selected Products", type="secondary"):
                        if len(selected_for_merge) >= 2:
                            merge_dialog(selected_for_merge)
                        else:
                            st.error("Please check the 'Merge' box on at least 2 products before clicking.")

                st.write("---")
                if st.button("💾 Save Product Info Changes", type="primary"):
                    try:
                        with get_db_connection() as conn:
                            cursor = conn.cursor()
                            for _, row in edited_prod_df.iterrows():
                                if pd.notna(row['ProductID']) and str(row['ProductID']).strip():
                                    cursor.execute('''
                                        UPDATE Products 
                                        SET Name = ?, BaseUnit = ?, Category = ?, SubCategory = ? 
                                        WHERE ProductID = ?
                                    ''', (row['Name'], row['BaseUnit'], row['Category'], row['SubCategory'], row['ProductID']))
                            conn.commit()
                        st.success("Product characteristics updated successfully!")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error updating products: {e}")

        # -------------------------------------------------------------------------
        # SUB-TAB 3.2: PRODUCT MAPPINGS TABLE
        # -------------------------------------------------------------------------
        with subtab3_2:
            st.write("Manage the single shared mapping table used across all CSV imports (Tab 1 & Tab 5).")

            mappings_df = get_all_mappings_df()

            if mappings_df.empty:
                st.info("No mappings saved yet. Map products in the Sales Entry or Market Importer tab to save them automatically.")
            else:
                db_prods = get_all_products()
                db_prod_options = db_prods['ProductID'].tolist() if not db_prods.empty else []

                edited_mappings_df_sub = st.data_editor(
                    mappings_df,
                    num_rows="dynamic",
                    use_container_width=True,
                    key="tab3_mappings_editor",
                    column_config={
                        "ImportName": st.column_config.TextColumn("CSV Product Name (ImportName)", required=True),
                        "ProductID": st.column_config.SelectboxColumn("Mapped DB ProductID", options=db_prod_options, required=True),
                        "DBProductName": st.column_config.TextColumn("Mapped DB Product Name", disabled=True),
                    }
                )

                if st.button("💾 Save Sub-Tab Mapping Changes", type="primary", key="save_tab3_mappings"):
                    try:
                        with get_db_connection() as conn:
                            cursor = conn.cursor()
                            cursor.execute("DELETE FROM ProductMappings")
                            for _, row in edited_mappings_df_sub.iterrows():
                                if pd.notna(row['ImportName']) and str(row['ImportName']).strip() != "":
                                    cursor.execute('''
                                        INSERT INTO ProductMappings (ImportName, ProductID) 
                                        VALUES (?, ?)
                                    ''', (str(row['ImportName']).strip(), str(row['ProductID']).strip()))
                            conn.commit()

                        st.success("Mappings updated successfully!")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Error updating mappings: {e}")


# =============================================================================
# TAB 4: CUSTOMER MANAGEMENT (LOCAL ONLY)
# =============================================================================
if is_local and tab_cust:
    with tab_cust:
        st.header("👥 Customer Management")
        st.write("View, create, edit, and merge customer records stored in the database.")

        with st.expander("➕ Add New Customer", expanded=False):
            with st.form("add_customer_form"):
                new_c_name = st.text_input("Customer Name (e.g. Restaurant Le Bistro)")
                submit_new_cust = st.form_submit_button("💾 Save New Customer")
                if submit_new_cust:
                    if not new_c_name.strip():
                        st.error("Please provide a valid Customer Name.")
                    else:
                        try:
                            get_or_create_customer(new_c_name.strip())
                            st.success(f"Customer '{new_c_name.strip()}' created successfully!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error creating customer: {e}")

        st.divider()

        with get_db_connection() as conn:
            cust_df = pd.read_sql_query("SELECT CustomerID, Name FROM Customers ORDER BY Name", conn)

        if cust_df.empty:
            st.info("No customers found in the database yet.")
        else:
            cust_df.insert(0, "Select for Merge", False)

            st.write("Edit customer names directly in the table below and click **Save Customer Changes**, OR select customers to **Merge**.")

            c_merge_btn, _ = st.columns([1, 2])

            edited_cust_df = st.data_editor(
                cust_df,
                num_rows="dynamic",
                use_container_width=True,
                key="customer_editor",
                column_config={
                    "Select for Merge": st.column_config.CheckboxColumn("Merge", default=False),
                    "CustomerID": st.column_config.NumberColumn("Customer ID", disabled=True),
                    "Name": st.column_config.TextColumn("Customer Name", required=True)
                }
            )

            selected_cust_for_merge = edited_cust_df[edited_cust_df["Select for Merge"] == True]

            with c_merge_btn:
                if st.button("🔗 Merge Selected Customers", type="secondary", key="btn_merge_cust"):
                    if len(selected_cust_for_merge) >= 2:
                        merge_customers_dialog(selected_cust_for_merge)
                    else:
                        st.error("Please check the 'Merge' box on at least 2 customers before clicking.")

            st.write("---")
            if st.button("💾 Save Customer Info Changes", type="primary", key="btn_save_cust"):
                try:
                    with get_db_connection() as conn:
                        cursor = conn.cursor()
                        for _, row in edited_cust_df.iterrows():
                            if pd.notna(row['CustomerID']) and str(row['Name']).strip():
                                cursor.execute('''
                                    UPDATE Customers 
                                    SET Name = ? 
                                    WHERE CustomerID = ?
                                ''', (str(row['Name']).strip(), int(row['CustomerID'])))
                        conn.commit()
                    st.success("Customer names updated successfully!")
                    st.rerun()
                except Exception as e:
                    st.error(f"Error updating customers: {e}")

with tab4:
    st.header("📦 Order Management")

    mode = st.radio(
        "Select Action:",
        ["📥 Import Market Order from CSV", "✏️ Search & Edit Existing Orders"],
        horizontal=True,
        key="tab4_action_mode"
    )

    st.divider()

    # -------------------------------------------------------------------------
    # MODE 1: IMPORT MARKET ORDER FROM CSV
    # -------------------------------------------------------------------------
    if mode == "📥 Import Market Order from CSV":
        st.subheader("📥 Import Market Sales CSV")
        st.write(
            "Upload a market sales CSV to automatically prefill dates, map products, aggregate duplicates, and register a new order.")

        try:
            db_products_df = get_all_products()
            product_display_list = ["++ Create New Product ++"] + (
                db_products_df['Display'].tolist() if not db_products_df.empty else [])
            display_to_id = {row['Display']: row['ProductID'] for _, row in
                             db_products_df.iterrows()} if not db_products_df.empty else {}
            id_to_display = {row['ProductID']: row['Display'] for _, row in
                             db_products_df.iterrows()} if not db_products_df.empty else {}
        except Exception:
            db_products_df = pd.DataFrame()
            product_display_list = ["++ Create New Product ++"]
            display_to_id, id_to_display = {}, {}

        if len(product_display_list) <= 1:
            st.info(
                "💡 Your database has no existing products. Unmapped items will automatically default to creating a new product.")

        uploaded_file = st.file_uploader("Upload market POS CSV", type=["csv"],
                                         key="market_csv_upload")

        extracted_date = datetime.date.today()
        if uploaded_file:
            date_matches = re.findall(r'(\d{4}-\d{2}-\d{2})', uploaded_file.name)
            if date_matches:
                try:
                    extracted_date = datetime.datetime.strptime(date_matches[0], "%Y-%m-%d").date()
                except ValueError:
                    pass

            if st.session_state.get("market_csv_name") != uploaded_file.name:
                st.session_state["market_date_placed"] = extracted_date
                st.session_state["market_date_fulfilled"] = extracted_date
                st.session_state["market_order_id"] = f"MARKET-{extracted_date.strftime('%Y%m%d')}"

        with st.container():
            c1, c2, c3, c4 = st.columns(4)
            default_order_id = f"MARKET-{extracted_date.strftime('%Y%m%d')}"
            new_order_id = c1.text_input("Order ID", value=default_order_id, key="market_order_id")
            new_customer = c2.text_input("Customer Name", value="Market",
                                         key="market_customer_name")
            new_date_placed = c3.date_input("Date Placed", value=extracted_date,
                                            key="market_date_placed")
            new_date_fulfilled = c4.date_input("Date Fulfilled", value=extracted_date,
                                               key="market_date_fulfilled")

        if uploaded_file and product_display_list:
            if "market_csv_name" not in st.session_state or st.session_state[
                "market_csv_name"] != uploaded_file.name:
                try:
                    uploaded_file.seek(0)
                    external_df = pd.read_csv(uploaded_file)
                except Exception:
                    uploaded_file.seek(0)
                    try:
                        external_df = pd.read_csv(uploaded_file, encoding='utf-16', sep='\t')
                    except Exception:
                        uploaded_file.seek(0)
                        external_df = pd.read_csv(uploaded_file, encoding='windows-1252', sep=';')

                parsed_df = parse_csv_to_scratch(external_df, db_products_df)

                parsed_df['Selected DB Product'] = parsed_df['ProductID'].apply(
                    lambda x: id_to_display.get(x, "++ Create New Product ++"))

                st.session_state["market_draft_df"] = parsed_df[
                    ['ImportName', 'Selected DB Product', 'BaseQuantitySold', 'SalePrice']]
                st.session_state["market_csv_name"] = uploaded_file.name

            st.subheader("Review & Map Products")
            st.info(
                "Multiple rows mapped to the same product will be automatically merged into a single sales line upon saving.")

            edited_market_df = st.data_editor(
                st.session_state["market_draft_df"],
                num_rows="dynamic",
                use_container_width=True,
                key="market_items_editor",
                column_config={
                    "ImportName": st.column_config.TextColumn("CSV Original Name", disabled=True),
                    "Selected DB Product": st.column_config.SelectboxColumn("Mapped DB Product",
                                                                            options=product_display_list,
                                                                            required=True),
                    "BaseQuantitySold": st.column_config.NumberColumn("Quantity Sold", step=0.1),
                    "SalePrice": st.column_config.NumberColumn("Sale Price ($)", step=1.0)
                }
            )

            st.divider()

            if st.button("🚀 Aggregate & Save Market Order", type="primary"):
                if not new_order_id.strip():
                    st.error("Please enter a valid Order ID.")
                elif edited_market_df["Selected DB Product"].isna().any() or edited_market_df[
                    "Selected DB Product"].eq(
                        "").any():
                    st.error("Please ensure all items are mapped to a Database Product.")
                else:
                    needs_creation = edited_market_df[
                        edited_market_df["Selected DB Product"] == "++ Create New Product ++"]

                    if not needs_creation.empty:
                        import_names_to_create = needs_creation["ImportName"].unique().tolist()
                        define_new_products_dialog(import_names_to_create)
                    else:
                        with st.spinner("Saving mappings and aggregating order items..."):
                            edited_market_df['FinalProductID'] = edited_market_df[
                                'Selected DB Product'].map(
                                display_to_id)

                            for _, row in edited_market_df.iterrows():
                                csv_name = row['ImportName']
                                p_id = row['FinalProductID']
                                if pd.notna(csv_name) and str(csv_name).strip() != "" and pd.notna(
                                        p_id):
                                    save_product_mapping(str(csv_name).strip(), str(p_id))

                            aggregated_df = edited_market_df.groupby('FinalProductID').agg({
                                'BaseQuantitySold': 'sum',
                                'SalePrice': 'sum'
                            }).reset_index()

                            try:
                                c_id = get_or_create_customer(new_customer)
                                get_or_create_order(new_order_id, c_id, str(new_date_placed),
                                                    str(new_date_fulfilled))

                                for _, row in aggregated_df.iterrows():
                                    create_sale(new_order_id, row['FinalProductID'],
                                                float(row['BaseQuantitySold']),
                                                float(row['SalePrice']))

                                st.success(
                                    f"🎉 Successfully created order `{new_order_id}` with {len(aggregated_df)} combined line items!")

                                del st.session_state["market_draft_df"]
                                del st.session_state["market_csv_name"]

                                if st.button("Return to Order Manager"):
                                    st.rerun()

                            except Exception as e:
                                st.error(f"Database error while saving order: {e}")

    # -------------------------------------------------------------------------
    # MODE 2: SEARCH & EDIT EXISTING ORDERS
    # -------------------------------------------------------------------------
    else:
        st.subheader("✏️ Search & Edit Existing Registered Orders")

        conn = sqlite3.connect(DB_FILENAME)
        registered_orders_df = pd.read_sql_query('''
                        SELECT o.OrderID, c.Name AS CustomerName, o.DatePlaced, o.DateFullfilled 
                        FROM Orders o
                        JOIN Customers c ON o.CustomerID = c.CustomerID
                        ORDER BY o.DateFullfilled DESC
                    ''', conn)

        registered_orders_df['DateFullfilled_Obj'] = pd.to_datetime(
            registered_orders_df['DateFullfilled'],
            errors='coerce',
            format='mixed'
        ).dt.date
        registered_orders_df['DateFullfilled_Obj'] = registered_orders_df[
            'DateFullfilled_Obj'].fillna(
            registered_orders_df['DateFullfilled']
        )

        order_products_df = pd.read_sql_query('''
                        SELECT s.OrderID, p.Name AS ProductName
                        FROM Sales s
                        JOIN Products p ON s.ProductID = p.ProductID
                    ''', conn)
        conn.close()

        if registered_orders_df.empty:
            st.info("No orders registered in the database yet.")
        else:
            st.markdown("### 🔍 Search & Filter Orders")

            f_col1, f_col2, f_col3 = st.columns(3)

            all_order_dates = sorted(
                registered_orders_df['DateFullfilled_Obj'].dropna().unique().tolist(),
                reverse=True, key=str)
            search_dates = f_col1.multiselect("Filter by Date Fulfilled", all_order_dates,
                                              default=[],
                                              key="search_edit_date")

            all_sold_products = sorted(order_products_df['ProductName'].dropna().unique().tolist())
            search_products = f_col2.multiselect("Filter by Product Contained", all_sold_products,
                                                 default=[],
                                                 key="search_edit_prod")

            all_order_customers = sorted(
                registered_orders_df['CustomerName'].dropna().unique().tolist())
            search_customers = f_col3.multiselect("Filter by Customer", all_order_customers,
                                                  default=[],
                                                  key="search_edit_cust")

            valid_order_ids = registered_orders_df['OrderID'].tolist()

            if search_dates:
                registered_orders_df = registered_orders_df[
                    registered_orders_df['DateFullfilled_Obj'].isin(search_dates)]
                valid_order_ids = registered_orders_df['OrderID'].tolist()

            if search_customers:
                registered_orders_df = registered_orders_df[
                    registered_orders_df['CustomerName'].isin(search_customers)]
                valid_order_ids = registered_orders_df['OrderID'].tolist()

            if search_products:
                matched_orders = \
                order_products_df[order_products_df['ProductName'].isin(search_products)][
                    'OrderID'].unique()
                valid_order_ids = [oid for oid in valid_order_ids if oid in matched_orders]
                registered_orders_df = registered_orders_df[
                    registered_orders_df['OrderID'].isin(valid_order_ids)]

            st.divider()

            if not valid_order_ids:
                st.warning("No orders match the selected filters.")
            else:
                def format_reg_order(o_id):
                    row = registered_orders_df[registered_orders_df['OrderID'] == o_id].iloc[0]
                    return f"Order {o_id} | {row['CustomerName']} | Fulfilled: {row['DateFullfilled']}"


                selected_reg_order = st.selectbox("Select Registered Order to Edit",
                                                  valid_order_ids,
                                                  format_func=format_reg_order)

                if selected_reg_order:
                    conn = sqlite3.connect(DB_FILENAME)
                    order_header = pd.read_sql_query('SELECT * FROM Orders WHERE OrderID = ?', conn,
                                                     params=(selected_reg_order,)).iloc[0]
                    sales_items_df = pd.read_sql_query('''
                                    SELECT s.SaleID, s.ProductID, p.Name AS ProductName, s.BaseQuantitySold, s.SalePrice
                                    FROM Sales s
                                    JOIN Products p ON s.ProductID = p.ProductID
                                    WHERE s.OrderID = ?
                                ''', conn, params=(selected_reg_order,))
                    conn.close()

                    all_cust_df = get_all_customers()
                    cust_names = all_cust_df['Name'].tolist()
                    current_cust_row = all_cust_df[
                        all_cust_df['CustomerID'] == order_header['CustomerID']]
                    current_cust_name = current_cust_row.iloc[0][
                        'Name'] if not current_cust_row.empty else (
                        cust_names[0] if cust_names else "Market")

                    st.subheader(f"Editing Order: {selected_reg_order}")

                    with st.form("edit_order_header_form"):
                        ec1, ec2, ec3 = st.columns(3)
                        new_cust_name = ec1.selectbox("Customer",
                                                      cust_names if cust_names else ["Market"],
                                                      index=cust_names.index(
                                                          current_cust_name) if cust_names and current_cust_name in cust_names else 0)

                        val_dp = safe_parse_date(order_header['DatePlaced'])
                        val_df = safe_parse_date(order_header['DateFullfilled'])
                        new_date_placed = ec2.date_input("Date Placed", value=val_dp)
                        new_date_fulfilled = ec3.date_input("Date Fulfilled", value=val_df)

                        update_header_btn = st.form_submit_button("💾 Update Order Details")
                        if update_header_btn:
                            try:
                                c_id = get_or_create_customer(new_cust_name)
                                get_or_create_order(selected_reg_order, c_id, str(new_date_placed),
                                                    str(new_date_fulfilled))
                                st.success("Order header details updated successfully!")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Error updating order: {e}")

                    st.markdown("#### Sales Line Items in this Order")
                    if sales_items_df.empty:
                        st.warning("No sales items found for this order.")
                    else:
                        edited_sales_df = st.data_editor(
                            sales_items_df,
                            num_rows="dynamic",
                            use_container_width=True,
                            key="sales_editor",
                            column_config={
                                "SaleID": st.column_config.NumberColumn("Sale ID", disabled=True),
                                "ProductID": st.column_config.TextColumn("Product ID"),
                                "ProductName": st.column_config.TextColumn("Product Name"),
                                "BaseQuantitySold": st.column_config.NumberColumn("Base Qty Sold",
                                                                                  step=0.1),
                                "SalePrice": st.column_config.NumberColumn("Sale Price ($)",
                                                                           step=1.0)
                            }
                        )

                        if st.button("💾 Save Sales Line Items Changes", type="primary"):
                            try:
                                conn = sqlite3.connect(DB_FILENAME)
                                cursor = conn.cursor()

                                existing_sale_ids = set(sales_items_df['SaleID'].tolist())
                                updated_sale_ids = set(edited_sales_df['SaleID'].dropna().tolist())

                                deleted_ids = existing_sale_ids - updated_sale_ids
                                for d_id in deleted_ids:
                                    cursor.execute("DELETE FROM Sales WHERE SaleID = ?", (d_id,))

                                for _, row in edited_sales_df.iterrows():
                                    if pd.notna(row['SaleID']) and row[
                                        'SaleID'] in existing_sale_ids:
                                        cursor.execute('''
                                                        UPDATE Sales 
                                                        SET ProductID = ?, BaseQuantitySold = ?, SalePrice = ?
                                                        WHERE SaleID = ?
                                                    ''', (row['ProductID'], row['BaseQuantitySold'],
                                                          row['SalePrice'], row['SaleID']))
                                    else:
                                        cursor.execute('''
                                                        INSERT INTO Sales (OrderID, ProductID, BaseQuantitySold, SalePrice)
                                                        VALUES (?, ?, ?, ?)
                                                    ''', (selected_reg_order, row['ProductID'],
                                                          row['BaseQuantitySold'],
                                                          row['SalePrice']))

                                conn.commit()
                                conn.close()
                                st.success("Sales items updated successfully!")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Error saving sales items: {e}")

                    st.divider()
                    if st.button("🗑️ Delete Entire Order", type="secondary"):
                        try:
                            conn = sqlite3.connect(DB_FILENAME)
                            cursor = conn.cursor()
                            cursor.execute("DELETE FROM Sales WHERE OrderID = ?",
                                           (selected_reg_order,))
                            cursor.execute("DELETE FROM Orders WHERE OrderID = ?",
                                           (selected_reg_order,))
                            conn.commit()
                            conn.close()
                            st.success(f"Order {selected_reg_order} deleted successfully!")
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error deleting order: {e}")