import io,tempfile,unittest
from PIL import Image
from comercial import assets

class Media(unittest.TestCase):
    def test_uniform_without_distortion_and_original_versions_preserved(self):
        with tempfile.TemporaryDirectory() as root:
            buf=io.BytesIO();Image.new('RGB',(200,600),'red').save(buf,format='PNG')
            first=assets.put(root,3,buf.getvalue(),'odoo','test')
            with Image.open(assets.path(root,first)) as im:
                self.assertEqual(im.size,(1000,1000))
                self.assertEqual(im.getpixel((500,500)),(255,0,0))
                self.assertEqual(im.getpixel((0,0)),(255,255,255))
            buf=io.BytesIO();Image.new('RGB',(500,500),'blue').save(buf,format='PNG')
            second=assets.put(root,3,buf.getvalue(),'manual','test')
            self.assertNotEqual(first['hash'],second['hash'])
            self.assertTrue(assets.path(root,first).exists())
            self.assertEqual(assets.index(root)[3]['source'],'manual')

    def test_invalid_image_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises((ValueError,OSError)):assets.put(root,1,b'not a photo','manual','test')
