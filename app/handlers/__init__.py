from app.handlers import admin, filters, ledger, menu, sessions, wallet

# admin + menu first, catch-all text handlers last
ROUTERS = (
    admin.router, menu.router, sessions.router, ledger.router, wallet.router, filters.router,
)