import unittest
import uuid
import json
from unittest.mock import MagicMock
from parameterized import parameterized
from pendulum import now, Duration
from d3a.models.area import Area
from d3a.models.strategy import BidEnabledStrategy
from d3a.models.strategy.external_strategies.load import LoadHoursExternalStrategy
from d3a.models.strategy.external_strategies.pv import PVExternalStrategy
from d3a.models.strategy.external_strategies.storage import StorageExternalStrategy
import d3a.models.strategy.external_strategies
from d3a.models.market.market_structures import Trade, Offer, Bid
from d3a_interface.constants_limits import GlobalConfig
from d3a_interface.constants_limits import ConstSettings
from d3a.constants import DATE_TIME_FORMAT
from d3a.d3a_core.singletons import external_global_statistics

d3a.models.strategy.external_strategies.ResettableCommunicator = MagicMock


class TestExternalMixin(unittest.TestCase):

    def _create_and_activate_strategy_area(self, strategy):
        self.config = MagicMock()
        self.config.max_panel_power_W = 160
        self.config.ticks_per_slot = 90
        GlobalConfig.end_date = GlobalConfig.start_date + Duration(days=1)
        self.area = Area(name="test_area", config=self.config, strategy=strategy,
                         external_connection_available=True)
        self.parent = Area(name="parent_area", children=[self.area])
        self.parent.activate()
        external_global_statistics(self.area, self.config.ticks_per_slot)
        strategy.connected = True
        market = MagicMock()
        market.time_slot = GlobalConfig.start_date
        self.parent.get_future_market_from_id = lambda _: market
        self.area.get_future_market_from_id = lambda _: market

    def tearDown(self) -> None:
        ConstSettings.IAASettings.MARKET_TYPE = 1

    def test_dispatch_tick_frequency_gets_calculated_correctly(self):
        self.external_strategy = LoadHoursExternalStrategy(100)
        self._create_and_activate_strategy_area(self.external_strategy)
        d3a.d3a_core.util.DISPATCH_EVENT_TICK_FREQUENCY_PERCENT = 20
        self.config.ticks_per_slot = 90
        external_global_statistics(self.area, self.config.ticks_per_slot)
        assert external_global_statistics.external_tick_counter._dispatch_tick_frequency == 18
        self.config.ticks_per_slot = 10
        external_global_statistics(self.area, self.config.ticks_per_slot)
        assert external_global_statistics.external_tick_counter._dispatch_tick_frequency == 2
        self.config.ticks_per_slot = 100
        external_global_statistics(self.area, self.config.ticks_per_slot)
        assert external_global_statistics.external_tick_counter._dispatch_tick_frequency == 20
        self.config.ticks_per_slot = 99
        external_global_statistics(self.area, self.config.ticks_per_slot)
        assert external_global_statistics.external_tick_counter._dispatch_tick_frequency == 19
        d3a.d3a_core.util.DISPATCH_EVENT_TICK_FREQUENCY_PERCENT = 50
        self.config.ticks_per_slot = 90
        external_global_statistics(self.area, self.config.ticks_per_slot)
        assert external_global_statistics.external_tick_counter._dispatch_tick_frequency == 45
        self.config.ticks_per_slot = 10
        external_global_statistics(self.area, self.config.ticks_per_slot)
        assert external_global_statistics.external_tick_counter._dispatch_tick_frequency == 5
        self.config.ticks_per_slot = 100
        external_global_statistics(self.area, self.config.ticks_per_slot)
        assert external_global_statistics.external_tick_counter._dispatch_tick_frequency == 50
        self.config.ticks_per_slot = 99
        external_global_statistics(self.area, self.config.ticks_per_slot)
        assert external_global_statistics.external_tick_counter._dispatch_tick_frequency == 49

    @parameterized.expand([
        [LoadHoursExternalStrategy(100)],
        [PVExternalStrategy(2, max_panel_power_W=160)],
        [StorageExternalStrategy()]
    ])
    def test_dispatch_event_tick_to_external_aggregator(self, strategy):
        d3a.d3a_core.util.DISPATCH_EVENT_TICK_FREQUENCY_PERCENT = 20
        self._create_and_activate_strategy_area(strategy)
        strategy.redis.aggregator.is_controlling_device = lambda _: True
        self.config.ticks_per_slot = 90
        strategy.event_activate()
        assert external_global_statistics.external_tick_counter._dispatch_tick_frequency == 18
        self.area.current_tick = 1
        strategy._dispatch_event_tick_to_external_agent()
        strategy.redis.aggregator.add_batch_tick_event.assert_not_called()
        self.area.current_tick = 17
        strategy._dispatch_event_tick_to_external_agent()
        strategy.redis.aggregator.add_batch_tick_event.assert_not_called()
        self.area.current_tick = 18
        strategy._dispatch_event_tick_to_external_agent()
        strategy.redis.aggregator.add_batch_tick_event.assert_called_once()
        assert strategy.redis.aggregator.add_batch_tick_event.call_args_list[0][0][0] == \
            self.area.uuid
        result = strategy.redis.aggregator.add_batch_tick_event.call_args_list[0][0][1]
        assert result == \
            {'market_slot': GlobalConfig.start_date.format(DATE_TIME_FORMAT),
             'slot_completion': '20%'}
        strategy.redis.reset_mock()
        strategy.redis.aggregator.add_batch_tick_event.reset_mock()
        self.area.current_tick = 35
        strategy._dispatch_event_tick_to_external_agent()
        strategy.redis.aggregator.add_batch_tick_event.assert_not_called()
        self.area.current_tick = 36
        strategy._dispatch_event_tick_to_external_agent()
        strategy.redis.aggregator.add_batch_tick_event.assert_called_once()
        assert strategy.redis.aggregator.add_batch_tick_event.call_args_list[0][0][0] == \
            self.area.uuid
        result = strategy.redis.aggregator.add_batch_tick_event.call_args_list[0][0][1]
        assert result == \
            {'market_slot': GlobalConfig.start_date.format(DATE_TIME_FORMAT),
             'slot_completion': '40%'}

    @parameterized.expand([
        [LoadHoursExternalStrategy(100)],
        [PVExternalStrategy(2, max_panel_power_W=160)],
        [StorageExternalStrategy()]
    ])
    def test_dispatch_event_tick_to_external_agent(self, strategy):
        d3a.d3a_core.util.DISPATCH_EVENT_TICK_FREQUENCY_PERCENT = 20
        self._create_and_activate_strategy_area(strategy)
        strategy.redis.aggregator.is_controlling_device = lambda _: False
        self.config.ticks_per_slot = 90
        strategy.event_activate()
        assert external_global_statistics.external_tick_counter._dispatch_tick_frequency == 18
        self.area.current_tick = 1
        strategy._dispatch_event_tick_to_external_agent()
        strategy.redis.publish_json.assert_not_called()
        self.area.current_tick = 17
        strategy._dispatch_event_tick_to_external_agent()
        strategy.redis.publish_json.assert_not_called()
        self.area.current_tick = 18
        strategy._dispatch_event_tick_to_external_agent()
        strategy.redis.publish_json.assert_called_once()
        assert strategy.redis.publish_json.call_args_list[0][0][0] == "test_area/events/tick"
        result = strategy.redis.publish_json.call_args_list[0][0][1]
        result.pop('area_uuid')
        assert result == \
            {'slot_completion': '20%',
             'market_slot': GlobalConfig.start_date.format(DATE_TIME_FORMAT), 'event': 'tick',
             'device_info': strategy._device_info_dict}

        strategy.redis.reset_mock()
        strategy.redis.publish_json.reset_mock()
        self.area.current_tick = 35
        strategy._dispatch_event_tick_to_external_agent()
        strategy.redis.publish_json.assert_not_called()
        self.area.current_tick = 36
        strategy._dispatch_event_tick_to_external_agent()
        strategy.redis.publish_json.assert_called_once()
        assert strategy.redis.publish_json.call_args_list[0][0][0] == "test_area/events/tick"
        result = strategy.redis.publish_json.call_args_list[0][0][1]
        result.pop('area_uuid')
        assert result == \
            {'slot_completion': '40%',
             'market_slot': GlobalConfig.start_date.format(DATE_TIME_FORMAT), 'event': 'tick',
             'device_info': strategy._device_info_dict}

    @parameterized.expand([
        [LoadHoursExternalStrategy(100),
         Bid('bid_id', now(), 20, 1.0, 'test_area')],
        [PVExternalStrategy(2, max_panel_power_W=160),
         Offer('offer_id', now(), 20, 1.0, 'test_area')],
        [StorageExternalStrategy(),
         Bid('bid_id', now(), 20, 1.0, 'test_area')],
        [StorageExternalStrategy(),
         Offer('offer_id', now(), 20, 1.0, 'test_area')]
    ])
    def test_dispatch_event_trade_to_external_aggregator(self, strategy, offer_bid):
        strategy._track_energy_sell_type = lambda _: None
        self._create_and_activate_strategy_area(strategy)
        strategy.redis.aggregator.is_controlling_device = lambda _: True
        market = self.area.get_future_market_from_id(1)
        self.area._markets.markets = {1: market}
        strategy.state._available_energy_kWh = {market.time_slot: 1000.0}
        strategy.state.pledged_sell_kWh = {market.time_slot: 0.0}
        strategy.state.offered_sell_kWh = {market.time_slot: 0.0}
        current_time = now()
        if isinstance(offer_bid, Bid):
            self.area.strategy.add_bid_to_posted(market.id, offer_bid)
            trade = Trade('id', current_time, offer_bid,
                          'parent_area', 'test_area', fee_price=0.23, seller_id=self.area.uuid,
                          buyer_id=self.parent.uuid)
        else:
            self.area.strategy.offers.post(offer_bid, market.id)
            trade = Trade('id', current_time, offer_bid,
                          'test_area', 'parent_area', fee_price=0.23, buyer_id=self.area.uuid,
                          seller_id=self.parent.uuid)

        strategy.event_trade(market_id="test_market", trade=trade)
        assert strategy.redis.aggregator.add_batch_trade_event.call_args_list[0][0][0] == \
            self.area.uuid

        call_args = strategy.redis.aggregator.add_batch_trade_event.call_args_list[0][0][1]
        assert set(call_args.keys()) == {'attributes', 'residual_bid_id', 'asset_id', 'buyer',
                                         'local_market_fee', 'residual_offer_id', 'total_fee',
                                         'traded_energy', 'bid_id', 'time', 'seller',
                                         'trade_price', 'trade_id', 'offer_id', 'event'}
        assert call_args['trade_id'] == trade.id
        assert call_args['asset_id'] == self.area.uuid
        assert call_args['event'] == 'trade'
        assert call_args['trade_price'] == 20
        assert call_args['traded_energy'] == 1.0
        assert call_args['total_fee'] == 0.23
        assert call_args['time'] == current_time.isoformat()
        assert call_args['residual_bid_id'] == 'None'
        assert call_args['residual_offer_id'] == 'None'
        if isinstance(offer_bid, Bid):
            assert call_args['bid_id'] == trade.offer.id
            assert call_args['offer_id'] == 'None'
            assert call_args['seller'] == trade.seller
            assert call_args['buyer'] == 'anonymous'
        else:
            assert call_args['bid_id'] == 'None'
            assert call_args['offer_id'] == trade.offer.id
            assert call_args['seller'] == 'anonymous'
            assert call_args['buyer'] == trade.buyer

    @parameterized.expand([
        [LoadHoursExternalStrategy(100)],
        [PVExternalStrategy(2, max_panel_power_W=160)],
        [StorageExternalStrategy()]
    ])
    def test_skip_dispatch_double_event_trade_to_external_agent_two_sided_market(self, strategy):
        ConstSettings.IAASettings.MARKET_TYPE = 2
        strategy._track_energy_sell_type = lambda _: None
        self._create_and_activate_strategy_area(strategy)
        market = self.area.get_future_market_from_id(1)
        self.area._markets.markets = {1: market}
        strategy.state._available_energy_kWh = {market.time_slot: 1000.0}
        strategy.state.pledged_sell_kWh = {market.time_slot: 0.0}
        strategy.state.offered_sell_kWh = {market.time_slot: 0.0}
        current_time = now()
        if isinstance(strategy, BidEnabledStrategy):
            bid = Bid('offer_id', now(), 20, 1.0, 'test_area')
            strategy.add_bid_to_posted(market.id, bid)
            skipped_trade = \
                Trade('id', current_time, bid, 'test_area', 'parent_area', fee_price=0.23)

            strategy.event_trade(market_id=market.id, trade=skipped_trade)
            call_args = strategy.redis.aggregator.add_batch_trade_event.call_args_list
            assert call_args == []

            published_trade = \
                Trade('id', current_time, bid, 'parent_area', 'test_area', fee_price=0.23)
            strategy.event_trade(market_id=market.id, trade=published_trade)
            assert strategy.redis.aggregator.add_batch_trade_event.call_args_list[0][0][0] == \
                self.area.uuid
        else:
            offer = Offer('offer_id', now(), 20, 1.0, 'test_area')
            strategy.offers.post(offer, market.id)
            skipped_trade = \
                Trade('id', current_time, offer, 'parent_area', 'test_area', fee_price=0.23)
            strategy.offers.sold_offer(offer, market.id)

            strategy.event_trade(market_id=market.id, trade=skipped_trade)
            call_args = strategy.redis.aggregator.add_batch_trade_event.call_args_list
            assert call_args == []

            published_trade =\
                Trade('id', current_time, offer, 'test_area', 'parent_area', fee_price=0.23)
            strategy.event_trade(market_id=market.id, trade=published_trade)
            assert strategy.redis.aggregator.add_batch_trade_event.call_args_list[0][0][0] == \
                self.area.uuid

    def test_device_info_dict_for_load_strategy_reports_required_energy(self):
        strategy = LoadHoursExternalStrategy(100)
        self._create_and_activate_strategy_area(strategy)
        strategy.state._energy_requirement_Wh[strategy.next_market.time_slot] = 0.987
        assert strategy._device_info_dict["energy_requirement_kWh"] == 0.000987

    def test_device_info_dict_for_pv_strategy_reports_available_energy(self):
        strategy = PVExternalStrategy(2, max_panel_power_W=160)
        self._create_and_activate_strategy_area(strategy)
        strategy.state._available_energy_kWh[strategy.next_market.time_slot] = 1.123
        assert strategy._device_info_dict["available_energy_kWh"] == 1.123

    def test_device_info_dict_for_storage_strategy_reports_battery_stats(self):
        strategy = StorageExternalStrategy(battery_capacity_kWh=0.5)
        self._create_and_activate_strategy_area(strategy)
        strategy.state.energy_to_sell_dict[strategy.next_market.time_slot] = 0.02
        strategy.state.energy_to_buy_dict[strategy.next_market.time_slot] = 0.03
        strategy.state._used_storage = 0.01
        assert strategy._device_info_dict["energy_to_sell"] == 0.02
        assert strategy._device_info_dict["energy_to_buy"] == 0.03
        assert strategy._device_info_dict["used_storage"] == 0.01
        assert strategy._device_info_dict["free_storage"] == 0.49

    @parameterized.expand([
        [LoadHoursExternalStrategy(100)],
        [PVExternalStrategy(2, max_panel_power_W=160)],
        [StorageExternalStrategy()]
    ])
    def test_register_device(self, strategy):
        self.config = MagicMock()
        self.device = Area(name="test_area", config=self.config, strategy=strategy)
        payload = {"data": json.dumps({"transaction_id": str(uuid.uuid4())})}
        self.device.strategy.owner = self.device
        assert self.device.strategy.connected is False
        self.device.strategy._register(payload)
        self.device.strategy.register_on_market_cycle()
        assert self.device.strategy.connected is True
        self.device.strategy._unregister(payload)
        self.device.strategy.register_on_market_cycle()
        assert self.device.strategy.connected is False

        payload = {"data": json.dumps({"transaction_id": None})}
        with self.assertRaises(ValueError):
            self.device.strategy._register(payload)
        with self.assertRaises(ValueError):
            self.device.strategy._unregister(payload)

    @parameterized.expand([
        [LoadHoursExternalStrategy(100)],
        [PVExternalStrategy(2, max_panel_power_W=160)],
        [StorageExternalStrategy()]
    ])
    def test_get_state(self, strategy):
        strategy.state.get_state = MagicMock(return_value={"available_energy": 500})
        strategy.connected = True
        strategy._use_template_strategy = True
        current_state = strategy.get_state()
        assert current_state['connected'] is True
        assert current_state['use_template_strategy'] is True
        assert current_state['available_energy'] == 500

    @parameterized.expand([
        [LoadHoursExternalStrategy(100)],
        [PVExternalStrategy(2, max_panel_power_W=160)],
        [StorageExternalStrategy()]
    ])
    def test_restore_state(self, strategy):
        strategy.state.restore_state = MagicMock()
        strategy.connected = True
        strategy._connected = True
        strategy._use_template_strategy = True
        state_dict = {
            "connected": False,
            "use_template_strategy": False,
            "available_energy": 123
        }
        strategy.restore_state(state_dict)
        assert strategy.connected is False
        assert strategy._connected is False
        assert strategy._use_template_strategy is False
        strategy.state.restore_state.assert_called_once_with(state_dict)
