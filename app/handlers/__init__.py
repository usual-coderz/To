from app.handlers import (admin, filters, ledger, menu, receivers, sell, sessions, wallet)

ROUTERS = (
    admin.router, menu.router, sessions.router, sell.router, receivers.router,
    ledger.router, wallet.router, filters.router,
)