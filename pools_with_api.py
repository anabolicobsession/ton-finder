from datetime import timedelta, timezone, datetime
from itertools import chain

from network import Pool, Network, Token, TimePeriodsData, DEX
from pool_with_chart import Tick
from pools import Pools
from api.geckoterminal_api import GeckoTerminalAPI, PoolSource, SortBy, Pool as DEXScreenerPool, Timeframe, Currency, \
    Candlestick as GeckoTerminalCandlestick
from api.dex_screener_api import DEXScreenerAPI
import settings


_NO_UPDATE = -1


def make_batches(sequence: list, n: int) -> list[list]:
    return [sequence[i:n + i] for i in range(0, len(sequence), n)]


class PoolsWithAPI(Pools):

    REQUESTS_RESET_TIMEOUT = timedelta(seconds=60)
    CHECK_FOR_NEW_TOKENS_EVERY_UPDATE = 20
    APPLY_FILTER_EVERY_UPDATE = 20

    def __init__(self, **params):
        super().__init__(**params)
        self.geckoterminal_api = GeckoTerminalAPI()
        self.dex_screener_api = DEXScreenerAPI()
        self.update_counter = 0
        self.last_chart_update: dict[Pool, int] = {}
        
    async def close_api_sessions(self):
        await self.geckoterminal_api.close()
        await self.dex_screener_api.close()

    def _increment_update_counter(self):
        self.update_counter += 1

    def _satisfy(self, every_update):
        return self.update_counter % every_update == 0

    @staticmethod
    def _dex_screener_pool_to_pool(p: DEXScreenerPool) -> Pool:
        return Pool(
            network=Network.from_id(p.network_id),
            address=p.address,
            base_token=Token(
                network=Network.from_id(p.network),
                address=p.base_token.address,
                ticker=p.base_token.ticker,
                name=p.base_token.name,
            ),
            quote_token=Token(
                network=Network.from_id(p.network),
                address=p.quote_token.address,
                ticker=p.quote_token.ticker,
                name=p.quote_token.name,
            ),

            price_usd=p.price_usd,
            price_native=p.price_native,
            liquidity=p.liquidity.total,
            volume=p.volume.h24,
            fdv=p.fdv,

            price_change=TimePeriodsData(
                m5=p.price_change.m5,
                h1=p.price_change.h1,
                h6=p.price_change.h6,
                h24=p.price_change.h24,
            ),
            dex=DEX(p.dex_id),
            creation_date=p.creation_date,
        )

    @staticmethod
    def _geckoterminal_candlestick_to_candlestick(c: GeckoTerminalCandlestick) -> Tick:
        return Tick(
            timestamp=c.timestamp,
            price=c.close,
            volume=c.volume,
        )

    async def update_using_api(self):
        if self._satisfy(PoolsWithAPI.APPLY_FILTER_EVERY_UPDATE):
            self.apply_filter()

        new_addresses = []
        if self._satisfy(PoolsWithAPI.CHECK_FOR_NEW_TOKENS_EVERY_UPDATE):

            for source in (PoolSource.TOP, PoolSource.TRENDING):
                new_addresses.extend(await self.geckoterminal_api.get_pools(
                    settings.NETWORK.get_id(),
                    pool_source=source,
                    pages=GeckoTerminalAPI.ALL_PAGES,
                    sort_by=SortBy.VOLUME,
                ))

        timestamp = datetime.now(timezone.utc)
        rounded_timestamp = timestamp - timedelta(
            seconds=timestamp.second,
            microseconds=timestamp.microsecond,
        )

        all_addresses = list(set([p.address for p in self]) | set(new_addresses))
        self.update(
            list(chain(*[
                map(
                    self._dex_screener_pool_to_pool,
                    await self.dex_screener_api.get_pools(settings.NETWORK.get_id(), batch)
                )
                for batch in make_batches(all_addresses, DEXScreenerAPI.MAX_ADDRESSES)
            ]))
        )

        # add the latest price to the chart, because GeckoTerminal (OHLCV) requests have quota
        for p in self:
            p.chart.update(Tick(rounded_timestamp, p.price_native))

        # update OHLCV of the most perspective pools (double-level sorting is used)
        priority_list = [
            (
                p,
                self.last_chart_update.get(p, _NO_UPDATE),
                p.volume * abs(p.price_change.h1),
            ) for p in self
        ]
        priority_list.sort(key=lambda t: (t[1], -t[2]))
        pools_for_chart_update = [t[0] for t in priority_list[:self.geckoterminal_api.get_requests_left()]]

        for pool in pools_for_chart_update:
            pool.chart.update([
                self._geckoterminal_candlestick_to_candlestick(c) for c in await self.geckoterminal_api.get_ohlcv(
                    settings.NETWORK.get_id(),
                    pool_address=pool.address,
                    timeframe=Timeframe.Minute.ONE,
                    currency=Currency.TOKEN,
                )
             ])
            self.last_chart_update[pool] = self.update_counter

        self._increment_update_counter()
        self.geckoterminal_api.reset_request_counter()
        self.dex_screener_api.reset_request_counter()
