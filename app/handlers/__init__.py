from app.handlers import (admin, filters, input, ledger, menu, prices, ready,
                          receivers, sessions, wallet)

ROUTERS = (
    admin.router, menu.router, prices.router, ready.router, sessions.router,
    receivers.router, ledger.router, wallet.router, filters.router, input.router,
)