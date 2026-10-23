// Package catalog popisuje, co na SPARKu existuje: profily (sady služeb, které se
// vejdou do paměti spolu), doplňky (služby, které se dají přidat k profilu) a
// projekty s pásmy priorit. Pravidla z incidentů žijí tady jako data, ne v kódu:
// služba, která v profilu není, v něm neběží.
package catalog

import (
	"fmt"
	"os"
	"sort"
	"time"

	"gopkg.in/yaml.v3"
)

// Profile je sada služeb, kterou broker umí nahodit jedním příkazem.
// Up musí sám zastavit všechno, co s profilem nejde dohromady (rag-schedule.sh
// to pro comfy/llm/gemma/director dělá); Down uklidí věci, o kterých ostatní
// profily nevědí (bench kontejnery).
type Profile struct {
	Services  []string `yaml:"services"`
	Up        string   `yaml:"up"`
	Down      string   `yaml:"down"`
	Detect    string   `yaml:"detect"` // exit 0 = tenhle profil právě běží
	UpTimeout Duration `yaml:"up_timeout"`
	// Cena přepnutí v minutách — plánovač při stejné prioritě dá přednost
	// úloze, která nepotřebuje přepínat.
	SwitchMin int `yaml:"switch_min"`
}

// Addon je služba, která se dá přidat k některým profilům (audio-music k comfy).
type Addon struct {
	Profiles []string `yaml:"profiles"`
	Up       string   `yaml:"up"`
	Down     string   `yaml:"down"`
	Detect   string   `yaml:"detect"`
	MemGiB   int      `yaml:"mem_gib"`
	MaxJobs  int      `yaml:"max_jobs"`
}

// Project je klient brokera; Max je strop priority, který si smí dát sám.
type Project struct {
	Max int `yaml:"max"`
}

// Recurring je přechodná pevná rezervace pro klienty, kteří broker zatím
// neumí volat (PromoClown heartbeat). Od..Do v místním čase, může přes půlnoc.
type Recurring struct {
	Name     string   `yaml:"name"`
	Project  string   `yaml:"project"`
	Services []string `yaml:"services"`
	From     string   `yaml:"from"`
	To       string   `yaml:"to"`
	Priority int      `yaml:"priority"`
}

type Catalog struct {
	Profiles      map[string]Profile `yaml:"profiles"`
	ProfileOrder  []string           `yaml:"profile_order"` // pořadí pro detekci a pro volbu cíle
	Addons        map[string]Addon   `yaml:"addons"`
	Projects      map[string]Project `yaml:"projects"`
	DefaultMax    int                `yaml:"default_project_max"`
	Recurring     []Recurring        `yaml:"recurring"`
	MemReserveGiB int                `yaml:"mem_reserve_gib"`
	Comfy         string             `yaml:"comfy_url"` // na hlídání nečinnosti uživatelských oken
	TempPauseC    int                `yaml:"temp_pause_c"`
	NotifyCmd     string             `yaml:"notify_cmd"`
	LogDir        string             `yaml:"log_dir"`
}

type Duration struct{ time.Duration }

func (d *Duration) UnmarshalYAML(n *yaml.Node) error {
	v, err := time.ParseDuration(n.Value)
	if err != nil {
		return err
	}
	d.Duration = v
	return nil
}

func Load(path string) (*Catalog, error) {
	b, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	return Parse(b)
}

func Parse(b []byte) (*Catalog, error) {
	var c Catalog
	if err := yaml.Unmarshal(b, &c); err != nil {
		return nil, err
	}
	if c.DefaultMax == 0 {
		c.DefaultMax = 30
	}
	if c.MemReserveGiB == 0 {
		c.MemReserveGiB = 8
	}
	if c.TempPauseC == 0 {
		c.TempPauseC = 87
	}
	if len(c.ProfileOrder) == 0 {
		for n := range c.Profiles {
			c.ProfileOrder = append(c.ProfileOrder, n)
		}
		sort.Strings(c.ProfileOrder)
	}
	for _, n := range c.ProfileOrder {
		p, ok := c.Profiles[n]
		if !ok {
			return nil, fmt.Errorf("profile_order: neznámý profil %q", n)
		}
		if p.UpTimeout.Duration == 0 {
			p.UpTimeout.Duration = 20 * time.Minute
			c.Profiles[n] = p
		}
	}
	for n, a := range c.Addons {
		for _, p := range a.Profiles {
			if _, ok := c.Profiles[p]; !ok {
				return nil, fmt.Errorf("addon %s: neznámý profil %q", n, p)
			}
		}
	}
	for _, r := range c.Recurring {
		if _, err := ParseClock(r.From); err != nil {
			return nil, fmt.Errorf("recurring %s: %w", r.Name, err)
		}
		if _, err := ParseClock(r.To); err != nil {
			return nil, fmt.Errorf("recurring %s: %w", r.Name, err)
		}
	}
	return &c, nil
}

// ParseClock "HH:MM" → minuty od půlnoci.
func ParseClock(s string) (int, error) {
	var h, m int
	if _, err := fmt.Sscanf(s, "%d:%d", &h, &m); err != nil || h < 0 || h > 23 || m < 0 || m > 59 {
		return 0, fmt.Errorf("čas %q není HH:MM", s)
	}
	return h*60 + m, nil
}

// Known říká, jestli služba existuje v nějakém profilu nebo jako doplněk.
func (c *Catalog) Known(svc string) bool {
	if _, ok := c.Addons[svc]; ok {
		return true
	}
	for _, p := range c.Profiles {
		for _, s := range p.Services {
			if s == svc {
				return true
			}
		}
	}
	return false
}

// Fits: umí profil p obsloužit služby svcs (sám nebo s doplňky)?
// Vrací doplňky, které je potřeba nahodit.
func (c *Catalog) Fits(p string, svcs []string) (addons []string, ok bool) {
	prof, found := c.Profiles[p]
	if !found {
		return nil, false
	}
	have := map[string]bool{}
	for _, s := range prof.Services {
		have[s] = true
	}
	for _, s := range svcs {
		if have[s] {
			continue
		}
		a, isAddon := c.Addons[s]
		if !isAddon || !contains(a.Profiles, p) {
			return nil, false
		}
		addons = append(addons, s)
	}
	return addons, true
}

// Target vybere profil pro služby: přednost má current, pak pořadí z katalogu.
func (c *Catalog) Target(current string, svcs []string) (string, bool) {
	if _, ok := c.Fits(current, svcs); ok && current != "" {
		return current, true
	}
	for _, p := range c.ProfileOrder {
		if _, ok := c.Fits(p, svcs); ok {
			return p, true
		}
	}
	return "", false
}

func (c *Catalog) ProjectMax(name string) int {
	if p, ok := c.Projects[name]; ok && p.Max > 0 {
		return p.Max
	}
	return c.DefaultMax
}

func contains(xs []string, x string) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}
