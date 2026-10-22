import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import smooth_jitter

class JitterTests(unittest.TestCase):
    def points(self, values):
        return [dict(ts=i*.5,status='ok',latency=v,ip='127.0.0.1') for i,v in enumerate(values)]
    def test_spike_is_smoothed_but_raw_delta_preserved(self):
        points = smooth_jitter(self.points([30,30,120,30,30]),.5)
        self.assertEqual(points[2]['delta'],90)
        self.assertEqual(points[2]['jitter'],5.625)
        self.assertAlmostEqual(points[3]['jitter'],10.8984,places=4)
        self.assertLess(points[4]['jitter'],points[3]['jitter'])
        self.assertEqual(points[2]['latency'],120)
    def test_loss_ip_change_and_gap_do_not_make_false_pairs(self):
        for change in ('loss','ip','gap'):
            points=self.points([10,90,10,90])
            if change=='loss':points[1].update(status='timeout',latency=None)
            if change=='ip':points[2]['ip']='127.0.0.2'
            if change=='gap':points[2]['ts']=100;points[3]['ts']=100.5
            result=smooth_jitter(points,.5)
            self.assertIsNone(result[2]['jitter'])
    def test_viewport_warmup_is_independent_of_reduction(self):
        points=self.points(([30]*19+[120])*30)
        whole=smooth_jitter(points,.5)
        warm=smooth_jitter(points[344:],.5)
        self.assertAlmostEqual(whole[600-1]['jitter'],warm[-1]['jitter'],places=3)
        reduced=whole[::3]
        self.assertEqual(reduced[100]['jitter'],whole[300]['jitter'])
if __name__=='__main__':unittest.main()
