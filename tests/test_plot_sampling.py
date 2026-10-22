import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from plot_sampling import chart_samples

class PlotSampling(unittest.TestCase):
 def test_pixel_limit_preserves_both_series_extrema_and_state(self):
  points=[(i,{'a':100 if i==1234 else -5 if i==3234 else 2,'b':200 if i==5234 else 1},'reconnecting' if 6234<=i<=6236 else 'live') for i in range(10000)]
  sampled=chart_samples(points,0,10000,200,['a','b']);times={p[0] for p in sampled}
  self.assertLess(len(sampled),1500)
  for ts in (0,9999,1234,3234,5234,6234,6236):self.assertIn(ts,times)
  self.assertEqual(points[1234][1],{'a':100,'b':1})
 def test_missing_values_and_real_time_gaps_remain_breaks(self):
  points=[(i,{'a':None if 100<=i<=110 else i},'live') for i in range(200)]
  points.extend((i,{'a':i},'live') for i in range(220,300))
  sampled=chart_samples(points,0,300,3,['a']);by_time={p[0]:p for p in sampled}
  for ts in (99,100,110,111,199,220):self.assertIn(ts,by_time)
  self.assertTrue(by_time[220][3]);self.assertFalse(by_time[111][3]);self.assertIsNone(by_time[100][1]['a'])
 def test_sampling_does_not_invent_breaks_in_continuous_data(self):
  points=[(i,{'a':i},'live') for i in range(1000)]
  sampled=chart_samples(points,0,1000,10,['a'])
  self.assertTrue(any(b[0]-a[0]>3 for a,b in zip(sampled,sampled[1:])))
  self.assertFalse(any(p[3] for p in sampled))
 def test_empty_and_single_point(self):
  self.assertEqual(chart_samples([],0,1,0,['a']),[])
  self.assertEqual(chart_samples([(1,{'a':None},'live')],0,1,0,['a']),[(1,{'a':None},'live',False)])

if __name__=='__main__':unittest.main()
