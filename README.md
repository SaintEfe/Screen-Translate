# Screen Translate

**Yapımcı: SaintEfe**

Windows 10/11 üzerinde çalışan, seçtiğiniz ekran bölgesini (ROI) belirli
aralıklarla yakalayıp içindeki İngilizce metni OCR ile okuyan ve Türkçeye
çeviren masaüstü uygulaması. Arayüz PySide6, yakalama `mss`, OCR
`pytesseract`, çeviri `googletrans` ile yapılır. API anahtarı gerekmez.

Ekran görüntüleri yerel kalır; yalnızca OCR ile tanınan metin çeviri için
Google servisine gönderilir. Bu nedenle internet bağlantısı gerekir.

## Gereksinimler

- Windows 10 veya 11 (öncelikli ve test edilen platform)
- Python 3.11 - 3.13 (bu proje Python 3.13.16 ile doğrulandı)
- Tesseract OCR 5.x + `eng` dil verisi
- İnternet bağlantısı (çeviri için)

## Dosya ağacı

```
Çeviri/
├── Screen Translate.exe # Portable tek dosya sürüm (Python gerekmez)
├── main.py              # Giriş noktası, DPI farkındalığı, olay döngüsü
├── engine.py            # ROI modeli, OCR keşfi, worker thread, önbellek
├── roi.py               # ROI çerçevesi, alan seçici, fiziksel ROI hesabı
├── ui.py                # Kontrol paneli ve çeviri penceresi
├── icon.ico             # Uygulama simgesi
├── version_info.txt     # EXE sürüm bilgisi (SaintEfe)
├── requirements.txt     # Sabitlenmiş bağımlılıklar
├── kurulum_tesseract.ps1 # Yönetici hakkı gerektirmeyen Tesseract kurulumu
├── README.md            # Bu dosya
├── .gitignore
└── tests/
    ├── test_engine.py   # Motor testleri (sahte OCR/çeviri)
    └── test_ui.py       # Arayüz testleri (offscreen)
```

## Portable EXE

`Screen Translate.exe` tek dosyalık portable sürümdür; Python kurulumu
gerektirmez. Klasörü kopyalayarak taşıyabilirsiniz.

- **Tesseract yine gereklidir.** EXE içine gömülmez; `TESSERACT_CMD` ortam
  değişkeni veya `C:\Program Files\Tesseract-OCR` yolu kullanılır.
  `kurulum_tesseract.ps1` bu değişkeni ayarlar.
- İlk açılışta tek dosya geçici klasöre açıldığı için açılış birkaç saniye
  sürebilir.
- Ayarlar (çerçeve opaklığı) `HKCU\Software\SaintEfe\ScreenTranslate`
  altında saklanır.

Yeniden derlemek için:

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --clean --onefile `
  --windowed --name "Screen Translate" --icon icon.ico `
  --add-data "icon.ico;." --version-file version_info.txt main.py
```

## Kurulum

PowerShell'de proje klasörüne gidin:

```powershell
cd "C:\Users\<kullanıcı>\OneDrive\Desktop\Çeviri"
python -m venv .venv
```

Sanal ortamı etkinleştirmek zorunlu değildir. Execution policy kısıtlarından
kaçınmak için doğrudan `.venv` içindeki Python'u kullanabilirsiniz:

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Tek tek kurmak isterseniz:

```powershell
.\.venv\Scripts\python.exe -m pip install googletrans==4.0.2 PySide6==6.12.0 mss==10.2.0 pytesseract==0.3.13 Pillow==13.0.0
```

Etkinleştirmeyi tercih ederseniz: `.\.venv\Scripts\Activate.ps1`

## Tesseract OCR kurulumu

### Tek komutla kurulum (önerilen, yönetici hakkı gerekmez)

Proje klasöründe:

```powershell
powershell -ExecutionPolicy Bypass -File .\kurulum_tesseract.ps1
```

Betik Tesseract 5.4'ü `%LOCALAPPDATA%\Programs\Tesseract-OCR` altına kurar,
`eng` dil verisini doğrular ve `TESSERACT_CMD` değişkenini kullanıcı ortamına
yazar. Kurulum dosyası `/D` hedefini yok sayarsa (varsayılan konuma kurulum)
betik `C:\Program Files\Tesseract-OCR` yolunu da kontrol eder ve bulduğu yolu
kullanır; betik ikinci çalıştırmada indirmeyi atlar. Farklı bir klasör için
`-Yol "D:\Araçlar\Tesseract-OCR"` ekleyin. Kurulumdan sonra terminali ve
uygulamayı yeniden başlatın.

### Elle kurulum

Resmî Tesseract belgeleri Windows için UB Mannheim derlemesini işaret eder:
<https://github.com/UB-Mannheim/tesseract/wiki>

1. Kurulum dosyasını indirip çalıştırın.
2. "Additional language data" adımında **English** (`eng`) verisini seçin.
3. Varsayılan kurulum yolu genellikle şudur:

```
C:\Program Files\Tesseract-OCR\tesseract.exe
```

Kullanıcıya özel kurulumda yol şu olabilir:

```
C:\Users\<kullanıcı>\AppData\Local\Programs\Tesseract-OCR\tesseract.exe
```

Uygulama Tesseract'ı şu sırayla arar: `TESSERACT_CMD` ortam değişkeni,
`PATH`, ardından yukarıdaki standart yollar. Açıkça verilen geçersiz bir yol
sessizce başka bir yolla değiştirilmez; hata arayüzde gösterilir.

Farklı bir yola kurduysanız (PowerShell, yalnızca bu oturum için):

```powershell
$env:TESSERACT_CMD = "D:\Araçlar\Tesseract-OCR\tesseract.exe"
```

Kalıcı hale getirmek için:

```powershell
[Environment]::SetEnvironmentVariable("TESSERACT_CMD", "D:\Araçlar\Tesseract-OCR\tesseract.exe", "User")
```

Kurulumu doğrulayın:

```powershell
& "C:\Program Files\Tesseract-OCR\tesseract.exe" --version
& "C:\Program Files\Tesseract-OCR\tesseract.exe" --list-langs
```

`--list-langs` çıktısında `eng` görünmelidir.

## Çalıştırma

```powershell
.\.venv\Scripts\python.exe main.py
```

Uygulama açıldığında kontrol paneli görünür. Panel görev çubuğunda yer alır,
böylece çeviri penceresi kapalıyken de erişilebilir.

## Kullanım

1. **Alan seç**: Her ekran karartılır; fareyle bir dikdörtgen sürükleyin.
   `Esc` seçimi iptal eder. Seçilen dikdörtgen doğrudan yakalama alanı olur.
2. **Başlat / Duraklat**: Alan seçilmeden başlatılamaz. `Space` yerel kısayol
   olarak duraklatır/devam ettirir (yalnızca panel odaktayken; global değildir).
3. **Kaldır**: Seçili alanı ve çerçevesini kaldırır, çalışmayı durdurur ve
   çeviri penceresini temizler. Yeni alan için yeniden **Alan seç** yapın.
4. **Kapat** veya `Ctrl+Q`: Worker durdurulur, yardımcı pencereler gizlenir.
5. **Tarama aralığı**: 0.50 - 2.00 saniye arasında seçilebilir; varsayılan
   0.75 saniyedir.
6. **Çerçeve opaklığı**: %15 - %100 arasında kaydırıcıyla ayarlanır; varsayılan
   %92'dir. Ayar `QSettings` ile saklanır ve sonraki açılışta korunur.

Çerçeveyi üst şeridinden sürükleyerek taşıyabilir, sağ alt tutamaçtan
boyutlandırabilirsiniz. Çerçeve seçildiği ekranın kullanılabilir alanı içinde
kalır. Başka bir monitörde çalışmak için yeniden **Alan seç** yapın.

Çeviri penceresi ROI'nin altında, üstünde, sağında veya solunda boş alana
yerleştirilir ve ROI ile asla çakışmaz (kendi çıktısını yeniden okumamak için).
Yeterli boş alan yoksa çeviri kontrol panelinde gösterilir ve durum satırı
alanı küçültmenizi önerir.

### Zamanlama ve gecikme

0.75 saniye yalnızca tarama aralığıdır; uçtan uca gecikme garanti edilmez.
Toplam süre OCR (en fazla 3 sn), ağ isteği (en fazla 8 sn), istekler arası en az
1 saniyelik aralık ve hata durumundaki geri çekilme ile artabilir.

### Ağ davranışı

- Her çeviri isteği `asyncio.wait_for` ile toplam 8 saniye ile sınırlıdır.
- İki çeviri isteği arasında en az 1 saniye beklenir; istek birikmesi olmaz.
- Hata durumunda üstel geri çekilme uygulanır (1, 2, 4 ... en fazla 30 saniye)
  ve son başarılı çeviri ekranda kalır. Aynı metin yeniden denenir.
- Başarılı çeviriler 128 kayıtlık LRU önbelleğinde tutulur; değişmeyen metin
  yeniden gönderilmez.
- `googletrans` resmî olmayan bir istemcidir, kararlılık garantisi yoktur ve
  zaman zaman servis tarafında engellenebilir. API anahtarı kullanmaz.
  Resmî bir çeviri API'sine geçmek için hem `engine.TranslationWorker`
  içindeki `_translate` metodunu hem de `_default_translator` istemci
  fabrikasını değiştirmek gerekir; bu sürümde resmî API uygulanmamıştır.

### Gizlilik

- Uygulama ekran görüntülerini arşivlemez ve normal çalışmada kalıcı olarak
  kaydetmez. `pytesseract`, OCR sırasında geçici dosya kullanabilir ve bunları
  temizler.
- Yalnızca OCR ile tanınan metin Google çeviri servisine gönderilir.
- Hata mesajları tanınan metni içermez; yalnızca hata türünü belirtir.
- Kendi pencerelerimiz Windows 10 2004+ üzerinde
  `SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE)` ile yakalamadan
  hariç tutulmaya çalışılır. Bu desteklenmezse ve kontrol paneli ROI ile
  çakışırsa uygulama duraklar ve paneli taşımanızı ister.

## Sorun giderme

- **"Tesseract bulunamadı" / "yol geçersiz"**: Tesseract'ı kurun veya
  `TESSERACT_CMD` değerini doğru yola ayarlayın. Uygulamayı yeniden başlatın.
  Kurulum hatası 5 saniyede birden fazla kez denenmez; Tesseract'ı kurduktan
  sonra uygulamayı yeniden başlatmak en hızlı çözümdür.
- **"`eng` dil verisi eksik"**: Kurulumda English verisini ekleyin veya
  `tesseract --list-langs` çıktısını kontrol edin.
- **OCR boş veya hatalı**: Küçük bölgelerde `--psm 6` uygun olmayabilir.
  `engine.OCR_CONFIG` değerini `--psm 7` (tek satır) veya `--psm 11`
  (seyrek metin) ile deneyin. Alanı metnin tamamını kapsayacak şekilde seçin,
  çok küçük alanlardan kaçının.
- **"Çeviri isteği başarısız oldu"**: İnternet bağlantısını ve servis
  erişilebilirliğini kontrol edin. Uygulama geri çekilme ile yeniden dener.
- **Metin kısaltıldı uyarısı**: OCR metni 4000 karakteri aştığında kısaltılır
  ve bu durum çeviri penceresinde açıkça belirtilir.
- **Panel ROI ile çakışıyor uyarısı**: Paneli ROI dışına taşıyın veya alanı
  küçültün. Windows yakalama hariç tutma desteği yoksa bu kontrol zorunludur.

## Sınırlamalar

- Korumalı içerik (DRM), bazı tam ekran oyunlar ve donanım hızlandırmalı
  yüzeyler yakalanamayabilir.
- Karışık DPI ve çok monitör düzenlerinde fiziksel ROI Windows'ta HWND
  tabanlı hesaplanır; yine de olağandışı ölçekleme düzenlerinde alanı birkaç
  piksel kaydırarak doğrulamanız önerilir.
- macOS ve Linux **test edilmemiştir**. Kod, Windows dışında ekran DPR'sine
  dayalı en iyi çaba dönüşümü kullanır; yerel Wayland'de ekran yakalama
  kısıtlıdır ve desteklenmez. Linux'ta yalnızca X11 varsayılmıştır.
- Çoklu monitörde seçim tek ekranla sınırlıdır; ROI tek ekranı aşamaz.

## Testler

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

Testler gerçek ağa çıkmaz ve gerçek Tesseract kurulumu gerektirmez; sahte OCR
ve çeviri istemcileri kullanılır. Arayüz testleri `QT_QPA_PLATFORM=offscreen`
ile çalışır.

Doğrulama durumu: motor ve arayüz testleri (64 test) bu makinede geçmiştir.
Gerçek `mss` yakalama ve Google çeviri isteği ayrıca denenmiştir. Tesseract 5.4
`C:\Program Files\Tesseract-OCR` altına kurulmuştur; gerçek OCR içeren uçtan
uca akış (görüntü -> OCR -> çeviri) bu makinede doğrulanmıştır. Karışık DPI
düzeni hâlâ doğrulanmamıştır.
