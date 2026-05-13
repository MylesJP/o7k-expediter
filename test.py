
from o7k import workpackage
wp_path = workpackage.create_wp('cinder', '26.0.0', 'https://example.com')
wp = workpackage.load(wp_path)
print(wp['target']['upstream_project'], wp['target']['upstream_version'])
print('Stamps:', wp['stamps'])
workpackage.add_stamp(wp, wp_path, stage='test', result='verified', 
detail='test stamp')
wp = workpackage.load(wp_path)
print('After stamp:', wp['stamps'])
