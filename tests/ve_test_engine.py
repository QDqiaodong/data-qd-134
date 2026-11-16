"""Unit tests for register interpretation and integer Wh allocation."""
import unittest
from venue_energy.engine import interpret, allocate, slice_interval, TariffBook
from venue_energy import engine

class InterpretTests(unittest.TestCase):
    def test_zero_wrap_unique(self):
        # 100 W meter, one hour, 50 Wh: only k=0 fits.
        self.assertEqual(interpret(1000,100,100,150,3600),50)

    def test_register_decrease_is_a_wrap_not_negative_energy(self):
        # 900 -> 100 with modulus 1000: k=0 gives -800 (rejected by k>=0),
        # k=1 gives 200 Wh; at 100 W for two hours 200 Wh is exactly feasible.
        self.assertEqual(interpret(1000,100,900,100,7200),200)

    def test_multiple_feasible_wraps_rejected(self):
        # modulus 100, limit 300 Wh over the interval: k=0,1,2,3 all feasible
        # when R1==R0, so no unique interpretation exists.
        self.assertIsNone(interpret(100,100,10,10,10800))

    def test_no_feasible_wrap_rejected_without_defaulting_to_zero(self):
        # 0 -> 900 means at least 900 Wh, but the meter allows only 100 Wh.
        self.assertIsNone(interpret(1000,100,0,900,3600))

    def test_power_bound_is_inclusive(self):
        # Exactly max_watts*dt/3600 Wh is feasible.
        self.assertEqual(interpret(1000,100,0,100,3600),100)

    def test_brute_force_agrees_with_enumeration(self):
        import random
        rng=random.Random(7)
        for _ in range(3000):
            modulus=rng.randint(1,500);watts=rng.randint(0,200)
            r0=rng.randrange(modulus);r1=rng.randrange(modulus);dt=rng.randint(1,7200)
            feasible=[]
            for k in range(0,1000):
                e=r1-r0+k*modulus
                if e>=0 and 3600*e<=watts*dt:feasible.append((k,e))
            expected=feasible[0][1] if len(feasible)==1 else None
            self.assertEqual(interpret(modulus,watts,r0,r1,dt),expected)

class AllocateTests(unittest.TestCase):
    def test_pieces_sum_to_energy_and_floor_proportional(self):
        pieces=allocate(10,[(0,3),(1,3),(2,4)])
        self.assertEqual(sum(pieces),10)
        self.assertEqual(pieces,[3,3,4])

    def test_remainders_go_in_time_order(self):
        # 10 Wh over three equal seconds: floors 3,3,3 and the leftover Wh
        # goes to the earliest segment.
        self.assertEqual(allocate(10,[(0,1),(1,1),(2,1)]),[4,3,3])
        self.assertEqual(allocate(11,[(0,1),(1,1),(2,1)]),[4,4,3])

    def test_zero_energy(self):
        self.assertEqual(allocate(0,[(0,5),(5,5)]),[0,0])

    def test_more_pieces_than_wh(self):
        # Only the earliest segments receive a single Wh.
        self.assertEqual(allocate(2,[(0,1),(1,1),(2,1),(3,1)]),[1,1,0,0])

class SliceTests(unittest.TestCase):
    def _book(self):
        return TariffBook({'v':[(946684800,946684800+7200,100,50),
                                (946684800+7200,4102444800,200,60)]})

    def test_tariff_boundary_split_preserves_total(self):
        t0=946684800;t1=t0+10800          # 3 h, 2h at rate100 + 1h at rate200
        pieces=list(slice_interval('v',t0,t1,90,self._book()))
        self.assertEqual(sum(p[5] for p in pieces),90)
        self.assertEqual([p[3] for p in pieces],[100,200])
        self.assertEqual([p[2] for p in pieces],[7200,3600])
        # 60 Wh then 30 Wh proportionally.
        self.assertEqual([p[5] for p in pieces],[60,30])

    def test_month_boundary_split(self):
        import calendar,datetime
        # Interval straddling 2000-02-01T00:00:00Z, flat tariff.
        t0=calendar.timegm((2000,1,31,23,0,0,0,0,0))
        t1=calendar.timegm((2000,2,1,1,0,0,0,0))
        book=TariffBook({'v':[(946684800,4102444800,100,50)]})
        pieces=list(slice_interval('v',t0,t1,4,book))
        self.assertEqual(len(pieces),2)
        self.assertEqual([(p[0],p[1]) for p in pieces],[(2000,1),(2000,2)])
        self.assertEqual(sum(p[5] for p in pieces),4)
        self.assertEqual([p[5] for p in pieces],[2,2])

if __name__=='__main__':unittest.main()
