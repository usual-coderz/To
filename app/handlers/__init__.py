from app.handlers import admin, filters, ledger, menu, sessions, wallet

ROUTERS = (
    admin.router, menu.router, sessions.router, ledger.router, wallet.router, filters.router,
)